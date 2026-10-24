"""Emit fixture.json: ground truth from the real BrainChess Python for core.test.mjs.

Run with the worktree venv (python-chess, numpy):
    .venv/bin/python web/brainchess/test/make_fixture.py   (from the repo root)
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

import chess  # noqa: E402

from brainsync.brainchess.prior import heuristic_scores, to_weights  # noqa: E402
from brainsync.brainchess.split import MoveSelector, expected_picks, split  # noqa: E402

OUT = Path(__file__).with_name("fixture.json")

FENS = {
    "start": chess.STARTING_FEN,
    "after e4 (ep square set)": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
    "italian": "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3",
    "both castles white": "r3k2r/pppq1ppp/2npbn2/2b1p3/2B1P3/2NPBN2/PPPQ1PPP/R3K2R w KQkq - 4 8",
    "both castles black": "r3k2r/pppq1ppp/2npbn2/2b1p3/2B1P3/2NPBN2/PPPQ1PPP/R3K2R b KQkq - 4 8",
    "white en passant": "rnbqkbnr/ppp1p1pp/8/3pPp2/8/8/PPPP1PPP/RNBQKBNR w KQkq f6 0 3",
    "black en passant": "rnbqkbnr/pppp1ppp/8/8/3PpP2/8/PPP1P1PP/RNBQKBNR b KQkq f3 0 3",
    "illegal en passant (pin)": "8/8/8/K2pP2r/8/8/8/7k w - d6 0 1",
    "simple promotion": "8/P7/8/8/8/8/k6K/8 w - - 0 1",
    "capture promotions": "1r1q4/P1P5/8/8/8/8/k6K/8 w - - 0 1",
    "black capture promotion": "8/8/8/8/8/k7/4p3/K4N2 b - - 0 1",
    "promotion with check": "3r3k/2P5/8/8/8/8/8/K7 w - - 0 1",
    "check evasions (bishop)": "rnbqkbnr/ppp2ppp/8/1B1pp3/4P3/8/PPPP1PPP/RNBQK1NR b KQkq - 1 3",
    "check evasions (knight)": "rnbqkbnr/pppp1ppp/5N2/4p3/8/8/PPPPPPPP/R1BQKBNR b KQkq - 0 3",
    "single legal move": "R6k/8/5K2/8/8/8/8/8 b - - 0 1",
    "KR vs K": "8/8/8/4k3/8/8/8/4K2R w K - 0 1",
    "KP vs K": "8/8/4k3/8/4P3/4K3/8/8 w - - 0 1",
    "KQ vs K": "8/5k2/8/8/3Q4/8/5K2/8 w - - 0 1",
    "stalemate traps": "7k/8/6Q1/8/8/8/8/K7 w - - 0 1",
    "mate in one": "r1bqkb1r/pppp1ppp/2n2n2/4p2Q/2B1P3/8/PPPP1PPP/RNB1K1NR w KQkq - 4 4",
    "pinned knight": "4k3/8/8/8/4r3/8/4N3/4K3 w - - 0 1",
    "castle through check": "r3k2r/8/8/8/8/8/6b1/R3K2R w KQkq - 0 1",
    "four knights disambiguation": "k7/8/8/3N1N2/8/3N1N2/8/K7 w - - 0 1",
    "QGD midgame": "r1bq1rk1/pp2bppp/2n1pn2/3p4/2PP4/2N1PN2/PP3PPP/R2QKB1R w KQ - 0 8",
    "english attack": "r2q1rk1/1b1nbppp/p2ppn2/1p6/3NP3/1BN1BP2/PPPQ2PP/2KR3R w - - 0 11",
    "dragon black": "r1bq1rk1/pp2ppbp/2np1np1/8/3NP3/2N1BP2/PPPQ2PP/R3KB1R b KQ - 3 9",
    "late midgame": "2r2rk1/pp3ppp/2n1b3/q2p4/3P4/P1NBP3/1P3PPP/R2Q1RK1 w - - 0 15",
    "opening boundary move 10": "r1bqk2r/ppp2ppp/2np1n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R w KQkq - 0 10",
    "opening boundary move 11": "r1bqk2r/ppp2ppp/2np1n2/2b1p3/2B1P3/2NP1N2/PPP2PPP/R1BQK2R w KQkq - 0 11",
}


def random_positions() -> dict[str, str]:
    """Positions from seeded random-ish games (heuristic moves with noise)."""
    out = {}
    for seed in (1, 2, 3, 4):
        rng = random.Random(seed)
        board = chess.Board()
        for ply in range(1, 61):
            if board.is_game_over():
                break
            scores = heuristic_scores(board)
            w = to_weights(scores, 80.0, 0.2)
            moves = list(w)
            board.push(rng.choices(moves, weights=[w[m] for m in moves])[0])
            if ply in (14, 33, 52) and not board.is_game_over():
                out[f"random game {seed} ply {ply}"] = board.fen()
    return out


def uci_map(d):
    return {m.uci(): float(w) for m, w in d.items()}


def settings(board: chess.Board):
    scores = heuristic_scores(board)
    legal = sorted(board.legal_moves, key=lambda m: m.uci())
    partial = {legal[0]: 5.0}
    if len(legal) > 1:
        partial[legal[1]] = 3.0
    if len(legal) > 2:
        partial[legal[2]] = 0.0
    return [
        ("uniform", {}, {}),
        ("prior", to_weights(scores), {}),
        ("peaked", to_weights(scores, 10.0, 0.0), {}),
        ("partial", partial, {}),
        ("prior tight no-confirm", to_weights(scores), {"confirm": False, "balance_tol": 0.0, "min_gap": 2}),
    ]


def state(sel: MoveSelector) -> dict:
    return {
        "stage": sel.stage,
        "groups": sel.groups(),
        "panels": list(sel.panels()) if sel.panels() is not None else None,
        "arrow": list(sel.arrow()) if sel.arrow() is not None else None,
        "prompt": sel.prompt(),
        "picks": sel.picks,
    }


def trace(board, weights, opts, move: chess.Move) -> dict:
    sel = MoveSelector(board, weights, **opts)
    steps = []
    while sel.stage != "done":
        st = state(sel)
        st["target"] = sel.target_for(move)
        steps.append(st)
        sel.choose(st["target"])
    steps.append(state(sel))
    assert sel.move == move
    return {"move": move.uci(), "steps": steps}


def back_trace(board, weights, opts, a: chess.Move, b: chess.Move) -> list[dict]:
    """Drive a to confirm, pick BACK, then drive b to done (only when confirm is on)."""
    sel = MoveSelector(board, weights, **opts)
    steps = []
    while sel.stage != "confirm":
        st = state(sel)
        st["target"] = sel.target_for(a)
        steps.append(st)
        sel.choose(st["target"])
    st = state(sel)
    st["target"] = 1
    steps.append(st)
    sel.choose(1)
    while sel.stage != "done":
        st = state(sel)
        st["target"] = sel.target_for(b)
        steps.append(st)
        sel.choose(st["target"])
    steps.append(state(sel))
    assert sel.move == b
    return steps


def square_weights(sel: MoveSelector, key) -> list[list]:
    out: dict[int, float] = {}
    for m, w in sel.weights.items():
        out[key(m)] = out.get(key(m), 0.0) + w
    return [[sq, w] for sq, w in out.items()]


def split_record(pairs, **kw):
    if len(pairs) < 2:
        return None
    g0, g1 = split(dict((sq, w) for sq, w in pairs), **kw)
    return [sorted(g0), sorted(g1)]


def position_record(name: str, fen: str) -> dict:
    board = chess.Board(fen)
    legal = sorted(m.uci() for m in board.legal_moves)
    scores = heuristic_scores(board)
    rec = {
        "name": name,
        "fen": fen,
        "legal": legal,
        "heuristic": {m.uci(): round(s, 6) for m, s in scores.items()},
        "toWeights": uci_map(to_weights(scores)),
        "toWeightsReply": uci_map(to_weights(scores, 60.0, 0.05)),
        "settings": [],
    }
    for sname, weights, opts in settings(board):
        sel = MoveSelector(board, weights, **opts)
        origins = square_weights(sel, lambda m: m.from_square)
        kw = {k: v for k, v in opts.items() if k in ("balance_tol", "min_gap")}
        moves_sorted = sorted(board.legal_moves, key=lambda m: m.uci())
        srec = {
            "name": sname,
            "weights": [[m.uci(), float(w)] for m, w in weights.items()],
            "opts": {"confirm": opts.get("confirm", True), "balanceTol": opts.get("balance_tol", 0.15),
                     "minGap": opts.get("min_gap", 1)},
            "resolved": uci_map(sel.weights),
            "origins": origins,
            "originSplit": split_record(origins, **kw),
            "expectedPicks": expected_picks(board, weights, **opts),
            "traces": [trace(board, weights, opts, m) for m in moves_sorted],
        }
        if opts.get("confirm", True) and len(moves_sorted) >= 1:
            srec["backTrace"] = {"a": moves_sorted[0].uci(), "b": moves_sorted[-1].uci(),
                                 "steps": back_trace(board, weights, opts, moves_sorted[0], moves_sorted[-1])}
        rec["settings"].append(srec)
    return rec


def split_cases() -> list[dict]:
    """Synthetic split() inputs, many with exact ties to exercise every tie-break."""
    rng = random.Random(7)
    cases = []
    for i in range(300):
        n = rng.choice([2, 2, 3, 4, 5, 6, 8, 10, 13, 16, 20, 30])
        sqs = rng.sample(range(64), n)
        kind = i % 3
        if kind == 0:
            ws = [1.0] * n
        elif kind == 1:
            ws = [float(rng.randint(1, 5)) for _ in range(n)]
        else:
            ws = [rng.random() ** 3 + 1e-6 for _ in range(n)]
        tol = rng.choice([0.0, 0.05, 0.15, 0.15, 0.3, 1.0])
        gap = rng.choice([0, 1, 1, 2, 3])
        pairs = [[s, w] for s, w in zip(sqs, ws)]
        g0, g1 = split(dict(zip(sqs, ws)), balance_tol=tol, min_gap=gap)
        cases.append({"weights": pairs, "balanceTol": tol, "minGap": gap, "groups": [sorted(g0), sorted(g1)]})
    return cases


def float_cases() -> dict:
    rng = random.Random(11)
    sums = []
    for _ in range(50):
        xs = [rng.random() * 10 ** rng.randint(-8, 3) for _ in range(rng.randint(1, 40))]
        sums.append({"xs": xs, "sum": sum(xs)})
    rounds = [1 / 1024, 3 / 1024, 0.5, 0.1234567895, 0.9999999995, 1e-10, 2.5e-9, 0.3333333333]
    rounds += [rng.random() for _ in range(30)]
    return {"sums": sums, "rounds": [[x, round(x, 9)] for x in rounds]}


def gate_cases() -> list[dict]:
    """The real MoveGate fed chosen predictions (fs = 256 so 0.25 s and 1.5 s are whole samples)."""
    from types import SimpleNamespace

    import numpy as np

    from brainsync.brainchess.gate import ABSTAIN, MoveGate
    from brainsync.online import Decision

    fs, step, window = 256, 64, 384
    cases = []
    for seed in range(6):
        rng = random.Random(100 + seed)
        dec = SimpleNamespace(fs=float(fs), n_window=window, votes=4, n_seen=0)
        cooldown_s = [0.5, 0.5, 0.0, 0.25, 1.0, 0.5][seed]
        gate = MoveGate(dec, votes=[4, 4, 3, 4, 2, 5][seed], cooldown_s=cooldown_s)
        ops = []
        for k in range(6, 400):
            end = k * step
            dec.n_seen = end
            if rng.random() < 0.03:
                at = end - rng.choice([0, 32, 64, 100])
                gate.screen_changed(at)
                ops.append({"op": "screen", "at": at / fs})
            r = rng.random()
            raw = 0 if r < 0.45 else 1 if r < 0.8 else ABSTAIN
            if seed == 0:   # long agreeing runs
                raw = 0 if (k // 9) % 3 == 0 else 1 if (k // 9) % 3 == 1 else ABSTAIN
            fired = gate.push(Decision(time=0.0, scores=np.zeros(2), raw=raw, committed=ABSTAIN,
                                       changed=False, end=end))
            ops.append({"op": "push", "end": end / fs, "raw": raw, "fired": None if fired == ABSTAIN else int(fired),
                        "pending": gate.pending, "streak": gate.streak})
        cases.append({"votes": gate.votes, "cooldown": cooldown_s, "window": window / fs, "step": step / fs,
                      "ops": ops, "stale": gate.stale, "nFired": gate.fired})
    return cases


def main() -> None:
    fens = dict(FENS)
    fens.update(random_positions())
    assert len(list(chess.Board(FENS["single legal move"]).legal_moves)) == 1
    positions = [position_record(name, fen) for name, fen in fens.items()]
    data = {"positions": positions, "splitCases": split_cases(), "floats": float_cases(), "gateCases": gate_cases()}
    OUT.write_text(json.dumps(data))
    n_traces = sum(len(s["traces"]) for p in positions for s in p["settings"])
    print(f"wrote {OUT}: {len(positions)} positions, {n_traces} traces, "
          f"{len(data['splitCases'])} split cases, {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
