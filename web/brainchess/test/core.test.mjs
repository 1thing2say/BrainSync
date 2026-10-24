// npm install && npm run fixture && npm test   (from this directory)
// Cross-checks core.js against fixture.json, which make_fixture.py writes from the real Python.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const C = require(join(here, "..", "core.js"));
const { Chess } = require("chess.js");
const FX = JSON.parse(readFileSync(join(here, "fixture.json"), "utf8"));

const close = (a, b, tol = 1e-6) => Math.abs(a - b) <= tol * Math.max(1, Math.abs(b));
const sorted = (s) => [...s].sort((a, b) => a - b);

/** Collects mismatches so one failing assertion reports the first few with context. */
function checker() {
  const errs = [];
  let n = 0;
  return {
    eq(actual, expected, ctx) {
      n++;
      try {
        assert.deepStrictEqual(actual, expected);
      } catch {
        if (errs.length < 15) errs.push(`${ctx}: got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`);
        else errs.length === 15 && errs.push("...");
      }
    },
    num(actual, expected, ctx, tol = 1e-6) {
      n++;
      if (!(typeof actual === "number" && close(actual, expected, tol)) && errs.length < 15) {
        errs.push(`${ctx}: got ${actual}, want ${expected}`);
      }
    },
    done(label) {
      assert.equal(errs.length, 0, `${label}: ${errs.length} mismatches of ${n}\n${errs.join("\n")}`);
      return n;
    },
  };
}

test("fixture is large enough", () => {
  assert.ok(FX.positions.length >= 25, `${FX.positions.length} positions`);
  for (const p of FX.positions) assert.equal(p.settings.length, 5);
});

test("float helpers match CPython sum() and round(x, 9)", () => {
  const ck = checker();
  for (const { xs, sum } of FX.floats.sums) ck.eq(C._internal.pySum(xs), sum, `sum ${xs.length}`);
  for (const [x, r] of FX.floats.rounds) ck.eq(C._internal.pyRound9(x), r, `round(${x}, 9)`);
  ck.done("floats");
});

test("squares", () => {
  assert.equal(C.sqIndex("a1"), 0);
  assert.equal(C.sqIndex("h8"), 63);
  assert.equal(C.sqIndex("e4"), 28);
  for (let i = 0; i < 64; i++) assert.equal(C.sqIndex(C.sqName(i)), i);
  assert.throws(() => C.sqIndex("i1"), RangeError);
  assert.throws(() => C.sqIndex(3), TypeError);
  assert.throws(() => C.sqName(64), RangeError);
  assert.throws(() => C.sqName("a"), TypeError);
});

test("split() matches split.py on 300 synthetic cases (ties included)", () => {
  const ck = checker();
  FX.splitCases.forEach((c, i) => {
    const [g0, g1] = C.split(new Map(c.weights), { balanceTol: c.balanceTol, minGap: c.minGap });
    ck.eq([sorted(g0), sorted(g1)], c.groups, `case ${i}`);
  });
  ck.done("split");
});

test("split() and cutOptions() validate like split.py", () => {
  assert.throws(() => C.split(new Map([[3, 1]])), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, 0]])), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, -1]])), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, NaN]])), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, "1"]])), TypeError);
  assert.throws(() => C.split(new Map([[3, 1], [64, 1]])), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], ["a", 1]])), TypeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, 1]]), { balanceTol: -1 }), RangeError);
  assert.throws(() => C.split(new Map([[3, 1], [4, 1]]), { minGap: -1 }), RangeError);
  assert.throws(() => C.split({ 3: 1, 4: 1 }), TypeError);
  assert.deepEqual(C.cutOptions([0, 9]), [{ axis: "file", after: 0 }, { axis: "rank", after: 0 }]);
  assert.deepEqual(C.cutOptions([0, 8, 16]).map((c) => c.axis + c.after), ["rank0", "rank1"]);
  assert.deepEqual(C.cutOptions([5]), []);
});

test("chess.js 0.10.3 and python-chess agree on legal moves for every fixture FEN", () => {
  const ck = checker();
  for (const p of FX.positions) {
    const chess = new Chess(p.fen);
    ck.eq(chess.moves({ verbose: true }).map(C.uciOf).sort(), p.legal, p.name);
  }
  ck.done("legal moves");
});

test("heuristicScores and toWeights match prior.py", () => {
  const ck = checker();
  for (const p of FX.positions) {
    const chess = new Chess(p.fen);
    const before = chess.fen();
    const hs = C.heuristicScores(chess);
    assert.equal(chess.fen(), before, "heuristicScores must leave the board as it was");
    ck.eq([...hs.keys()].sort(), Object.keys(p.heuristic).sort(), `${p.name} heuristic keys`);
    for (const [m, s] of Object.entries(p.heuristic)) ck.num(hs.get(m), s, `${p.name} heuristic ${m}`);
    const w = C.toWeights(hs);
    for (const [m, x] of Object.entries(p.toWeights)) ck.num(w.get(m), x, `${p.name} toWeights ${m}`, 1e-9);
    const wr = C.toWeights(hs, { temperatureCp: 60, uniformMix: 0.05 });
    for (const [m, x] of Object.entries(p.toWeightsReply)) ck.num(wr.get(m), x, `${p.name} reply weights ${m}`, 1e-9);
  }
  ck.done("prior");
});

function stateOf(sel) {
  return {
    stage: sel.stage,
    groups: sel.groups(),
    panels: sel.panels(),
    arrow: sel.arrow(),
    prompt: sel.prompt(),
    picks: sel.picks,
  };
}

function runSteps(ck, chess, weights, opts, steps, ctx) {
  const sel = new C.MoveSelector(chess, weights, opts);
  steps.forEach((want, i) => {
    const { target, ...wantState } = want;
    ck.eq(stateOf(sel), wantState, `${ctx} step ${i}`);
    if (target !== undefined) sel.choose(target);
  });
  return sel;
}

test("MoveSelector traces, splits and expectedPicks match split.py for every legal move", () => {
  const ck = checker();
  let traces = 0;
  for (const p of FX.positions) {
    const chess = new Chess(p.fen);
    for (const s of p.settings) {
      const ctx = `${p.name} / ${s.name}`;
      const weights = new Map(s.weights);
      const opts = s.opts;
      const sel = new C.MoveSelector(chess, weights, opts);
      for (const [m, w] of Object.entries(s.resolved)) ck.num(sel.weights.get(m), w, `${ctx} resolved ${m}`, 1e-12);
      // split() on the exact from-square weights the Python split
      const origins = new Map(s.origins);
      if (s.originSplit === null) ck.eq(origins.size, 1, `${ctx} one origin`);
      else {
        const [g0, g1] = C.split(origins, { balanceTol: opts.balanceTol, minGap: opts.minGap });
        ck.eq([sorted(g0), sorted(g1)], s.originSplit, `${ctx} origin split`);
      }
      if (sel.stage === "piece") {
        const cands = sel.candidates();
        ck.eq(sorted(cands.keys()), sorted(origins.keys()), `${ctx} piece candidates`);
        for (const [sq, w] of origins) ck.num(cands.get(sq), w, `${ctx} candidate ${sq}`, 1e-12);
      }
      ck.num(C.expectedPicks(chess, weights, opts), s.expectedPicks, `${ctx} expectedPicks`);
      for (const tr of s.traces) {
        traces++;
        const t = new C.MoveSelector(chess, weights, opts);
        tr.steps.forEach((want, i) => {
          const { target, ...wantState } = want;
          ck.eq(stateOf(t), wantState, `${ctx} ${tr.move} step ${i}`);
          if (target !== undefined) {
            ck.eq(t.targetFor(tr.move), target, `${ctx} ${tr.move} targetFor at step ${i}`);
            t.choose(target);
          }
        });
        ck.eq(t.move, tr.move, `${ctx} ${tr.move} chosen`);
      }
      if (s.backTrace) {
        const b = runSteps(ck, chess, weights, opts, s.backTrace.steps, `${ctx} BACK ${s.backTrace.a}->${s.backTrace.b}`);
        ck.eq(b.move, s.backTrace.b, `${ctx} BACK final move`);
      }
    }
  }
  const n = ck.done("selector");
  assert.ok(traces >= 25 * 10, `${traces} traces`);
  console.log(`# selector: ${traces} traces, ${n} comparisons`);
});

test("MoveSelector API details", () => {
  const chess = new Chess(FX.positions.find((p) => p.name === "capture promotions").fen);
  const sel = new C.MoveSelector(chess, new Map());
  assert.equal(sel.stage, "piece");
  assert.throws(() => sel.choose(2), RangeError);
  assert.throws(() => sel.choose("0"), TypeError);
  assert.throws(() => new C.MoveSelector(chess, new Map([["e2e4", 1]])), RangeError);
  assert.throws(() => new C.MoveSelector(chess, new Map([["a7a8q", -1]])), RangeError);
  assert.throws(() => new C.MoveSelector(chess, new Map(), { balanceTol: -0.1 }), RangeError);
  assert.throws(() => new C.MoveSelector(new Chess("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"), new Map()), RangeError);
  // drive c7d8q, checking sideOf / reachable / candidates along the way
  const move = "c7d8q";
  while (sel.stage !== "done") {
    if (sel.stage === "piece") assert.equal(sel.sideOf(C.sqIndex("c7")), sel.targetFor(move));
    if (sel.stage === "promo") {
      assert.deepEqual(sel.candidates().sort(), ["c7d8b", "c7d8n", "c7d8q", "c7d8r"].filter((m) => sel.reachable(m)).sort());
      assert.equal(sel.sideOf(10), null);
      assert.ok(sel.promoGroups()[0].length >= 1);
    }
    sel.choose(sel.targetFor(move));
  }
  assert.equal(sel.move, move);
  assert.equal(sel.prompt(), "Playing cxd8=Q");
  assert.deepEqual(sel.arrow(), [C.sqIndex("c7"), C.sqIndex("d8")]);
  assert.throws(() => sel.choose(0), Error);
  const picks = sel.picks;
  sel.back();
  assert.equal(sel.stage, "piece");
  assert.equal(sel.picks, picks);
  assert.equal(sel.move, null);
  // plain-object weights are accepted too
  assert.equal(new C.MoveSelector(new Chess(), { e2e4: 1 }).weights.get("e2e4"), 1);
});

test("MoveGate step (MockDecoder.push) matches gate.py MoveGate on chosen predictions", () => {
  const ck = checker();
  FX.gateCases.forEach((c, ci) => {
    const d = new C.MockDecoder({ step: c.step, window: c.window, votes: c.votes, cooldown: c.cooldown });
    c.ops.forEach((op, i) => {
      if (op.op === "screen") d.screenChanged(op.at);
      else {
        const ev = d.push(op.end, op.raw);
        ck.eq([ev.fired, d.pending, d.streak], [op.fired, op.pending, op.streak], `gate case ${ci} op ${i}`);
      }
    });
    ck.eq([d.stale, d.fired], [c.stale, c.nFired], `gate case ${ci} counters`);
  });
  ck.done("gate");
});

test("MockDecoder: a perfect decoder fires exactly 2.75 s after a screen change", () => {
  const d = new C.MockDecoder({ pCorrect: 1, pWrong: 0 });
  d.screenChanged(0);
  assert.equal(d.earliestPick, 2.75);
  const ev = d.advance(10, 0);
  // windows end at 1.5, 1.75 (stale: they start before 0.5), 2.0, 2.25, 2.5, 2.75 (fires)
  assert.deepEqual(ev.map((e) => e.t), [1.5, 1.75, 2.0, 2.25, 2.5, 2.75]);
  assert.deepEqual(ev.map((e) => e.fresh), [false, false, true, true, true, true]);
  assert.deepEqual(ev.map((e) => e.streak), [0, 0, 1, 2, 3, 4]);
  const fire = ev[ev.length - 1];
  assert.equal(fire.fired, 0);
  assert.equal(fire.t, 2.75);
  assert.equal(d.lastT, 2.75, "advance stops at the pick");
  assert.equal(d.stale, 2);
  assert.equal(d.fired, 1);
  assert.equal(d.pending, C.ABSTAIN);
  assert.equal(d.streak, 0);
  assert.equal(d.switchT, 2.75, "a pick is a screen change at its window's end");
  // the next pick, looking at target 1, is another 2.75 s later
  const ev2 = d.advance(10, 1);
  assert.equal(ev2[ev2.length - 1].fired, 1);
  assert.equal(ev2[ev2.length - 1].t, 5.5);
  // off-grid change: first fresh window ends at ceil((t0 + 2.0) / 0.25) * 0.25
  d.screenChanged(5.6);
  assert.equal(d.firstFreshEnd, 7.75);
  const ev3 = d.advance(20, 0);
  assert.equal(ev3[ev3.length - 1].t, 8.5);
  assert.equal(d.earliestPick, 8.5 + 2.75);
  // frame-by-frame driving gives the same pick time
  const f = new C.MockDecoder({ pCorrect: 1, pWrong: 0 });
  let t = 0;
  let firedAt = null;
  while (firedAt === null && t < 10) {
    t += 1 / 60;
    for (const e of f.advance(t, 1)) if (e.fired !== null) firedAt = e.t;
  }
  assert.equal(firedAt, 2.75);
});

test("MockDecoder: stale windows are ignored", () => {
  const d = new C.MockDecoder({ pCorrect: 1, pWrong: 0 });
  d.advance(2.5, 0); // fresh windows at 2.0, 2.25, 2.5: streak 3
  assert.equal(d.streak, 3);
  d.screenChanged(2.5); // redraw: the run restarts and windows must start >= 3.0
  const ev = d.advance(6.0, 0);
  assert.deepEqual(ev.filter((e) => !e.fresh).map((e) => e.t), [2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.25]);
  assert.ok(ev.filter((e) => !e.fresh).every((e) => e.fired === null && e.streak === 0));
  assert.equal(ev[ev.length - 1].fired, 0);
  assert.equal(ev[ev.length - 1].t, 2.5 + 2.75);
});

test("MockDecoder: looking at nothing never fires when pStray = 0", () => {
  const d = new C.MockDecoder({ pStray: 0, rng: C.mulberry32(5) });
  const ev = d.advance(600, null);
  assert.equal(ev.length, (600 - 1.5) / 0.25 + 1);
  assert.ok(ev.every((e) => e.raw === C.ABSTAIN && e.fired === null));
  assert.equal(d.fired, 0);
  // with pStray = 1 the board-studying user does get false picks
  const s = new C.MockDecoder({ pStray: 1, rng: C.mulberry32(5) });
  let fires = 0;
  for (let t = 0.25; t <= 600; t += 0.25) fires += s.advance(t, null).filter((e) => e.fired !== null).length;
  assert.ok(fires > 10, `${fires} stray picks`);
});

test("MockDecoder: errors, wrong picks and determinism", () => {
  assert.throws(() => new C.MockDecoder({ step: 0 }), RangeError);
  assert.throws(() => new C.MockDecoder({ window: -1 }), RangeError);
  assert.throws(() => new C.MockDecoder({ votes: 1.5 }), RangeError);
  assert.throws(() => new C.MockDecoder({ cooldown: -0.1 }), RangeError);
  assert.throws(() => new C.MockDecoder({ pCorrect: 1.2 }), RangeError);
  assert.throws(() => new C.MockDecoder({ pCorrect: 0.8, pWrong: 0.3 }), RangeError);
  assert.throws(() => new C.MockDecoder({ pStray: -1 }), RangeError);
  assert.throws(() => new C.MockDecoder({ rng: 3 }), TypeError);
  assert.throws(() => new C.MockDecoder().advance(1, 2), RangeError);
  assert.throws(() => new C.MockDecoder().push(2, 5), RangeError);
  const w = new C.MockDecoder({ pCorrect: 0, pWrong: 1 });
  const ev = w.advance(10, 0);
  assert.equal(ev[ev.length - 1].fired, 1);
  const run = (seed) => {
    const d = new C.MockDecoder({ rng: C.mulberry32(seed) });
    const out = [];
    for (let t = 0.1; t < 60; t += 0.1) out.push(...d.advance(t, t < 30 ? 0 : 1));
    return out;
  };
  assert.deepEqual(run(9), run(9));
  assert.notDeepEqual(run(9), run(10));
  assert.equal(new C.MockDecoder().advance(-1, 0).length, 0);
  // gaze may be a function of the window end
  const g = new C.MockDecoder({ pCorrect: 1, pWrong: 0 });
  const ev2 = g.advance(10, (t) => (t < 2.4 ? 1 : 0));
  assert.equal(ev2[ev2.length - 1].fired, 0);
  assert.equal(ev2[ev2.length - 1].t, 3.25);
});

test("mulberry32 and opponentReply", () => {
  const r = C.mulberry32(42);
  const xs = Array.from({ length: 1000 }, r);
  assert.ok(xs.every((x) => x >= 0 && x < 1));
  assert.deepEqual(Array.from({ length: 5 }, C.mulberry32(42)), xs.slice(0, 5));
  const chess = new Chess(FX.positions.find((p) => p.name === "mate in one").fen);
  const counts = {};
  const rng = C.mulberry32(1);
  for (let i = 0; i < 200; i++) {
    const m = C.opponentReply(chess, rng);
    counts[m] = (counts[m] || 0) + 1;
  }
  assert.ok(counts.h5f7 > 180, `mate in one played ${counts.h5f7}/200`);
  assert.throws(() => C.opponentReply(new Chess("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1"), rng), RangeError);
});

/** One seeded game: the user plays white through selector + decoder, the heuristic plays black. */
function playGame(seed, decoderOpts) {
  const chess = new Chess();
  const userRng = C.mulberry32(seed * 3 + 1);
  const oppRng = C.mulberry32(seed * 3 + 2);
  const dec = new C.MockDecoder({ rng: C.mulberry32(seed * 3 + 3), ...decoderOpts });
  const step = dec.step;
  let t = 0;
  let plies = 0;
  let picks = 0;
  let userMoves = 0;
  let wrongPicks = 0;
  let backs = 0;
  const userSeconds = [];
  while (!chess.game_over() && plies < 40) {
    if (chess.turn() === "w") {
      const sel = new C.MoveSelector(chess, C.toWeights(C.heuristicScores(chess)));
      const intended = C.opponentReply(chess, userRng);
      dec.screenChanged(t);
      const start = t;
      while (sel.stage !== "done") {
        t += step;
        if (t - start > 600) throw new Error(`deadlock: no move after 600 s at ${chess.fen()} (${sel.stage})`);
        const look = C.userTarget(sel, intended);
        for (const e of dec.advance(t, look)) {
          if (e.fired === null) continue;
          if (e.fired !== look) wrongPicks++;
          if (sel.stage === "confirm" && e.fired === 1) backs++;
          sel.choose(e.fired);
          picks++;
        }
      }
      assert.ok(chess.move({ from: sel.move.slice(0, 2), to: sel.move.slice(2, 4), promotion: sel.move[4] }));
      userMoves++;
      userSeconds.push(t - start);
    } else {
      const m = C.opponentReply(chess, oppRng);
      assert.ok(chess.move({ from: m.slice(0, 2), to: m.slice(2, 4), promotion: m[4] }));
      t += 2.0; // the board holds still while the reply is shown
    }
    plies++;
  }
  return { plies, picks, userMoves, wrongPicks, backs, userSeconds, over: chess.game_over(), pgn: chess.pgn() };
}

test("seeded full games (selector + decoder + opponent) finish or reach 40 plies without deadlock", () => {
  const perfect = playGame(1, { pCorrect: 1, pWrong: 0 });
  assert.ok(perfect.over || perfect.plies === 40);
  assert.equal(perfect.wrongPicks, 0);
  // a perfect decoder picks every 2.75 s exactly
  for (const s of perfect.userSeconds) assert.ok(Math.abs(s / 2.75 - Math.round(s / 2.75)) < 1e-9, `${s}`);
  const stats = [];
  for (const seed of [1, 2, 3, 4, 5]) {
    const g = playGame(seed, {});
    assert.ok(g.over || g.plies === 40, `seed ${seed}: ${g.plies} plies`);
    assert.ok(g.userMoves >= 1);
    stats.push(g);
  }
  const moves = stats.reduce((a, g) => a + g.userMoves, 0);
  const picks = stats.reduce((a, g) => a + g.picks, 0);
  const secs = stats.reduce((a, g) => a + g.userSeconds.reduce((x, y) => x + y, 0), 0);
  const wrong = stats.reduce((a, g) => a + g.wrongPicks, 0);
  const perPick = perfect.picks / perfect.userMoves;
  console.log(`# perfect decoder: ${perfect.userMoves} moves, ${perPick.toFixed(2)} picks/move, ` +
    `${(perfect.userSeconds.reduce((x, y) => x + y, 0) / perfect.userMoves).toFixed(1)} s/move`);
  console.log(`# default decoder (5 games): ${moves} moves, ${(picks / moves).toFixed(2)} picks/move, ` +
    `${(secs / moves).toFixed(1)} s/move, ${(100 * wrong / picks).toFixed(1)}% wrong picks`);
  assert.ok(perPick > 2 && perPick < 9, `${perPick} picks per move`);
});

test("pyLegalMoves follows python-chess legal-move order on every fixture FEN", () => {
  const ck = checker();
  for (const p of FX.positions) {
    const chess = new Chess(p.fen);
    // fixture heuristic dicts keep python-chess legal_moves order
    ck.eq(C.pyLegalMoves(chess).map(C.uciOf), Object.keys(p.heuristic), p.name);
    ck.eq([...C.heuristicScores(chess).keys()], Object.keys(p.heuristic), `${p.name} heuristicScores order`);
    ck.eq([...new C.MoveSelector(chess, new Map()).weights.keys()], Object.keys(p.heuristic), `${p.name} selector order`);
  }
  ck.done("python move order");
});

test("simulated user breaks weight ties like game.py SimulatedUser (python-chess order)", () => {
  // python: MoveSelector(b, to_weights(heuristic_scores(b))).choose(1) leaves g8f6, f8d6 and
  // f8c5 tied for the heaviest move; max(candidate_moves) picks g8f6 (first in legal order)
  const chess = new Chess("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5N2/PPPP1PPP/RNBQK2R b KQkq - 3 3");
  const sel = new C.MoveSelector(chess, C.toWeights(C.heuristicScores(chess)));
  sel.choose(1);
  assert.equal(sel.stage, "piece");
  const w = sel.candidateMoves();
  assert.equal(w.get("g8f6"), w.get("f8d6"));
  assert.equal(w.get("g8f6"), w.get("f8c5"));
  assert.ok(!sel.reachable("d7d5"));
  assert.equal(C.userMove(sel, "d7d5"), "g8f6");
  assert.equal(C.userTarget(sel, "d7d5"), sel.targetFor("g8f6"));
  assert.equal(sel.targetFor("g8f6"), 1); // as python target_for
  assert.equal(C.userMove(sel, "g8e7"), "g8e7"); // reachable: keeps the intended move
});

function playLine(sans, fen) {
  const chess = new Chess(fen);
  const seen = new Map();
  const bump = () => { const k = C.positionKey(chess); seen.set(k, (seen.get(k) || 0) + 1); };
  bump();
  const out = [];
  for (const san of sans) {
    assert.ok(chess.move(san), san);
    bump();
    out.push(C.gameOutcome(chess, seen));
  }
  return { chess, seen, out };
}

test("gameOutcome ends a threefold repetition that chess.js 0.10.3 misses after a double push", () => {
  // game.py game_outcome() returns THREEFOLD_REPETITION right after 5.Ng1
  const line = ["e4", "Nf6", "Nf3", "Ng8", "Ng1", "Nf6", "Nf3", "Ng8", "Ng1"];
  const { chess, out } = playLine(line);
  assert.deepEqual(out.slice(0, -1), Array(line.length - 1).fill(null));
  assert.deepEqual(out.at(-1), { termination: "threefold_repetition", winner: null });
  assert.equal(chess.in_threefold_repetition(), false, "chess.js counts the unusable e3 square");
  // the en-passant square stays in the key only when the capture is legal
  assert.ok(C.positionKey(new Chess("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1")).endsWith(" -"));
  const ep = FX.positions.find((p) => p.name === "white en passant").fen;
  assert.equal(C.positionKey(new Chess(ep)).split(" ")[3], ep.split(" ")[3]);
});

test("gameOutcome: mate, stalemate, insufficient material, fifty moves", () => {
  const mate = playLine(["f3", "e5", "g4", "Qh4#"]);
  assert.deepEqual(mate.out.at(-1), { termination: "checkmate", winner: "b" });
  const stale = new Chess("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1");
  assert.equal(C.gameOutcome(stale, new Map()).termination, "stalemate");
  assert.equal(C.gameOutcome(new Chess("8/8/4k3/8/8/3K4/8/8 w - - 0 1"), new Map()).termination, "insufficient_material");
  assert.equal(C.gameOutcome(new Chess("8/8/4k3/8/8/3K4/8/R7 w - - 100 80"), new Map()).termination, "fifty_moves");
  assert.equal(C.gameOutcome(new Chess("8/8/4k3/8/8/3K4/8/R7 w - - 99 80"), new Map()), null);
  assert.equal(C.gameOutcome(new Chess(), undefined), null);
});
