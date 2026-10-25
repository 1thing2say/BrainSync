// node --test cvep.test.mjs   (from this directory; needs no npm install, unlike core.test.mjs)
// The c-VEP side of the browser BrainChess: the cvep.js copy, Back (undo) against split.py,
// the simulated user with Back, and the cycle-based MockCodeDecoder.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, "..");
const C = require(join(web, "core.js"));
const K = require(join(web, "cvep.js"));
const TRACE = JSON.parse(readFileSync(join(here, "undo_trace.json"), "utf8"));

/** The start position's 20 moves as chess.js 0.10.3 lists them (verbose), no chess.js needed. */
function startPosition() {
  const moves = [];
  for (const [from, tos] of [["b1", ["a3", "c3"]], ["g1", ["f3", "h3"]]]) {
    for (const to of tos) moves.push({ color: "w", from, to, piece: "n", flags: "n", san: "N" + to });
  }
  for (const f of "abcdefgh") {
    moves.push({ color: "w", from: f + "2", to: f + "3", piece: "p", flags: "n", san: f + "3" });
    moves.push({ color: "w", from: f + "2", to: f + "4", piece: "p", flags: "b", san: f + "4" });
  }
  return {
    moves: () => moves.map((m) => ({ ...m })),
    fen: () => "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    in_check: () => false,
  };
}

function uniformSelector(opts) {
  const chess = startPosition();
  const weights = new Map(chess.moves().map((m) => [m.from + m.to, 1.0]));
  return new C.MoveSelector(chess, weights, { confirm: true, ...opts });
}

test("web/brainchess/cvep.js is a byte-identical copy of clients/js/cvep.js", () => {
  const a = readFileSync(join(web, "cvep.js"));
  const b = readFileSync(join(web, "..", "..", "clients", "js", "cvep.js"));
  assert.ok(a.equals(b), "copy clients/js/cvep.js to web/brainchess/cvep.js again");
  assert.equal(K.N_TARGETS, 3);
  assert.equal(K.LAG_FRAMES, 21);
});

test("index.html loads cvep.js before core.js and its own script", () => {
  const html = readFileSync(join(web, "index.html"), "utf8");
  const i = html.indexOf('<script src="cvep.js"></script>');
  assert.ok(i > 0 && i < html.indexOf('<script src="core.js"></script>'));
});

test("Back (undo) follows split.py step by step", () => {
  const sel = uniformSelector({ undo: true });
  const row = () => [sel.stage, sel.groups(), sel.panels(), sel.stage === "done" ? null : sel.prompt(), sel.canUndo];
  assert.deepEqual(row(), TRACE.trace[0]);
  TRACE.script.forEach((t, i) => {
    if (t === C.BACK) sel.undo();
    else sel.choose(t);
    assert.deepEqual(row(), TRACE.trace[i + 1], `after pick ${i} (${t})`);
  });
  assert.equal(sel.picks, TRACE.picks);
  assert.equal(sel.undos, TRACE.undos);
});

test("undo: nothing at the first pick, refused once chosen; freq mode keeps PLAY / BACK", () => {
  const sel = uniformSelector({ undo: true });
  assert.equal(sel.canUndo, false);
  assert.equal(sel.undo(), false);
  assert.equal(sel.picks, 1);
  const g = "g1f3";
  while (sel.stage !== "done") sel.choose(sel.targetFor(g));
  assert.equal(sel.move, g);
  assert.throws(() => sel.undo(), /already chosen/);
  const freq = uniformSelector({});
  while (freq.stage !== "confirm") freq.choose(freq.targetFor(g));
  assert.deepEqual(freq.panels(), ["PLAY", "BACK"]);
  assert.match(freq.prompt(), /Look at PLAY or BACK$/);
});

test("userLook: Back after a wrong pick, then the right group again", () => {
  const sel = uniformSelector({ undo: true });
  const want = "e2e4";
  const right = C.userLook(sel, want);
  assert.ok(right === 0 || right === 1);
  sel.choose(1 - right);
  assert.equal(C.userLook(sel, want), C.BACK);
  sel.undo();
  assert.equal(C.userLook(sel, want), right);
  // an error-prone user with Back still gets its move
  const rng = C.mulberry32(5);
  const s2 = uniformSelector({ undo: true });
  for (let i = 0; i < 300 && s2.stage !== "done"; i++) {
    let t = C.userLook(s2, want);
    if (rng() < 0.25) t = (t + 1 + Math.floor(rng() * 2)) % 3;
    if (t === C.BACK) s2.undo();
    else s2.choose(t);
  }
  assert.equal(s2.move, want);
});

test("MockCodeDecoder: a perfect decoder picks 3.65 s after a change, dropping the first cycle", () => {
  const d = new C.MockCodeDecoder({ pCorrect: 1, pWrong: 0, rng: C.mulberry32(1) });
  assert.equal(d.firstCycle, 1);
  assert.ok(Math.abs(d.earliestPick - 3.65) < 1e-9);
  const ev = d.advance(10, 2);
  assert.deepEqual(ev.map((e) => [e.fresh, e.raw, e.fired]), [[false, -1, null], [true, 2, null], [true, 2, 2]]);
  assert.ok(Math.abs(ev[2].t - 3.65) < 1e-9 && d.lastT === ev[2].t);
  assert.equal(d.stale, 1);
  assert.equal(d.switchT, ev[2].t); // the pick is a screen change
  assert.equal(ev[2].changed, true);
  assert.equal(ev[1].scores.length, 3);
  assert.ok(ev[1].scores[2] > 0.4 && ev[1].lead > 0.3);
  // the next pick needs the same three cycles from the new screen
  const next = d.advance(20, 0);
  assert.ok(Math.abs(next[next.length - 1].t - (ev[2].t + 3.65)) < 1e-9);
});

test("MockCodeDecoder: looking at nothing with pStray 0 never picks; validation; determinism", () => {
  const d = new C.MockCodeDecoder({ pStray: 0, rng: C.mulberry32(2) });
  const ev = d.advance(60, null);
  assert.ok(ev.length > 40 && ev.every((e) => e.fired === null && e.raw === C.ABSTAIN));
  const run = (seed) => {
    const x = new C.MockCodeDecoder({ pCorrect: 0.6, pWrong: 0.2, rng: C.mulberry32(seed) });
    const out = [];
    for (let t = 1; t < 60; t += 1) for (const e of x.advance(t, 1)) out.push([e.raw, e.fired]);
    return out;
  };
  assert.deepEqual(run(7), run(7));
  assert.ok(run(7).some(([, f]) => f !== null && f !== 1), "a noisy decoder makes wrong picks");
  assert.throws(() => new C.MockCodeDecoder({ votes: 0 }), /votes/);
  assert.throws(() => new C.MockCodeDecoder({ pCorrect: 0.9, pWrong: 0.2 }), /pCorrect \+ pWrong/);
  assert.throws(() => new C.MockCodeDecoder().advance(5, 3), /gaze/);
});

test("the confidence lines are the standard's (as brainsync.vep.live)", () => {
  // python: confidence_labels(d, ("A", "B", "BACK")) and confidence_line(d, ...) for
  // scores (0.42, 0.11, -0.03), raw 0, committed 0, changed, gap 0.31
  const labels = ["A", "B", "BACK"];
  const width = Math.max(...labels.map((s) => s.length));
  const lines = [0.42, 0.11, -0.03].map((s, k) => K.labelLine(labels[k], s, k === 0, width));
  assert.deepEqual(lines, ["A    ███░░░░░ +0.42 <", "B    █░░░░░░░ +0.11", "BACK ░░░░░░░░ -0.03"]);
  assert.equal(K.statusLine(0.31, "A", true), "lead 0.31   choice: A *");
});
