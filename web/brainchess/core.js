/* BrainChess core: a JavaScript port of brainsync/brainchess split.py (straight-cut
 * two-target move selection), prior.py (heuristic move scores, prior, opponent) and the
 * gate.py fresh-window pick rule, plus a simulated SSVEP decoder for the browser mockup.
 * The c-VEP standard (docs/CVEP-STANDARD.md, the default of python -m brainsync.brainchess)
 * adds a third target, Back: MoveSelector({undo: true}) undoes the last pick, userLook()
 * is game.py's SimulatedUser with Back, and MockCodeDecoder simulates CodeGate +
 * CodeVepDecoder (one decision per 1.05 s code cycle, scores per target, 2 votes).
 *
 * Pure logic, no DOM. Works in the browser (global BrainChessCore) and in Node
 * (module.exports). Chess rules come from a chess.js 0.10.3 instance passed in.
 *
 * Squares follow python-chess: 0..63, a1 = 0, h8 = 63, file = sq % 8, rank = sq >> 3.
 * Moves are UCI strings ('e2e4', 'e7e8q').
 */
(function (root) {
  "use strict";

  const BOARD = 8;
  const AXES = ["file", "rank"];
  const STAGES = Object.freeze(["piece", "dest", "promo", "confirm", "done"]);
  const FLOOR_SHARE = 1e-3;
  const EPS = 1e-12; // float slack when comparing imbalances (split.py _EPS)
  const ABSTAIN = -1;
  const PROMO_NAMES = Object.freeze({ q: "Queen", r: "Rook", b: "Bishop", n: "Knight" });
  // python-chess piece type numbers, used for the promotion tie-break
  const PIECE_TYPE = Object.freeze({ p: 1, n: 2, b: 3, r: 4, q: 5, k: 6 });

  // prior.py constants
  const MATE_SCORE = 10000.0;
  const PIECE_VALUES = Object.freeze({ p: 100, n: 320, b: 330, r: 500, q: 900, k: 0 });
  const CHECK_BONUS = 40.0;
  const CASTLE_BONUS = 60.0;
  const DEVELOP_BONUS = 30.0;
  const EARLY_KING_PENALTY = 40.0;
  const EARLY_QUEEN_PENALTY = 15.0;
  const STALEMATE_PENALTY = 300.0;
  const CENTRE_CP = 10.0;
  const OPENING_MOVES = 10;
  const REPLY_TEMPERATURE_CP = 60.0;
  const REPLY_UNIFORM_MIX = 0.05;

  // ---------------------------------------------------------------------------------
  // Float helpers that reproduce CPython 3.12 / numpy arithmetic, so near-ties between
  // cuts resolve the same way as in the Python.

  /** CPython 3.12 sum() of floats: start 0, first item added exactly, then Neumaier. */
  function pySum(values) {
    let first = true;
    let f = 0;
    let c = 0;
    for (const x of values) {
      if (first) {
        f = 0 + x;
        first = false;
        continue;
      }
      const t = f + x;
      if (Math.abs(f) >= Math.abs(x)) c += (f - t) + x;
      else c += (x - t) + f;
      f = t;
    }
    if (c !== 0 && Number.isFinite(c)) f += c;
    return f;
  }

  /** numpy float64 add.reduce of a contiguous 1-D array (pairwise summation). */
  function npSum(a) {
    function pw(lo, n) {
      if (n < 8) {
        let res = 0.0;
        for (let i = 0; i < n; i++) res += a[lo + i];
        return res;
      }
      if (n <= 128) {
        const r = [a[lo], a[lo + 1], a[lo + 2], a[lo + 3], a[lo + 4], a[lo + 5], a[lo + 6], a[lo + 7]];
        let i = 8;
        for (; i < n - (n % 8); i += 8) {
          for (let j = 0; j < 8; j++) r[j] += a[lo + i + j];
        }
        let res = ((r[0] + r[1]) + (r[2] + r[3])) + ((r[4] + r[5]) + (r[6] + r[7]));
        for (; i < n; i++) res += a[lo + i];
        return res;
      }
      let n2 = Math.floor(n / 2);
      n2 -= n2 % 8;
      return pw(lo, n2) + pw(lo + n2, n - n2);
    }
    return 0.0 + pw(0, a.length);
  }

  const _f64 = new Float64Array(1);
  const _u32 = new Uint32Array(_f64.buffer);
  const _LITTLE = new Uint8Array(new Uint16Array([1]).buffer)[0] === 1;

  /** True when finite x >= 0 lies exactly halfway between two 9-decimal numbers. */
  function _tie9(x) {
    _f64[0] = x;
    const hi = _u32[_LITTLE ? 1 : 0];
    const lo = _u32[_LITTLE ? 0 : 1];
    const bexp = (hi >>> 20) & 0x7ff;
    let mant = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
    let e;
    if (bexp === 0) e = -1074;
    else {
      mant |= 1n << 52n;
      e = bexp - 1075;
    }
    if (mant === 0n) return false;
    // x * 2e9 = mant * 2^e * 2 * 10^9 must be an odd integer
    let num = mant * 2000000000n;
    if (e >= 0) num <<= BigInt(e);
    else {
      const d = 1n << BigInt(-e);
      if (num % d !== 0n) return false;
      num /= d;
    }
    return (num & 1n) === 1n;
  }

  /** Python round(x, 9) for finite x >= 0 (round half to even on the exact value). */
  function pyRound9(x) {
    let s = x.toFixed(9); // exact value, ties go up
    if (_tie9(x)) {
      const digits = BigInt(s.replace(".", ""));
      if (digits % 2n === 1n) {
        const d = (digits - 1n).toString().padStart(10, "0");
        s = d.slice(0, d.length - 9) + "." + d.slice(d.length - 9);
      }
    }
    return Number(s);
  }

  // ---------------------------------------------------------------------------------
  // Squares and moves

  function checkSquare(sq) {
    if (typeof sq !== "number") throw new TypeError(`square must be an integer 0..63, got ${typeof sq} ${String(sq)}`);
    if (!Number.isInteger(sq) || sq < 0 || sq >= BOARD * BOARD) {
      throw new RangeError(`square must be an integer 0..63, got ${sq}`);
    }
  }

  function sqIndex(name) {
    if (typeof name !== "string") throw new TypeError(`square name must be a string like 'e4', got ${typeof name}`);
    if (!/^[a-h][1-8]$/.test(name)) throw new RangeError(`not a square name: ${JSON.stringify(name)}`);
    return (name.charCodeAt(0) - 97) + 8 * (name.charCodeAt(1) - 49);
  }

  function sqName(i) {
    checkSquare(i);
    return String.fromCharCode(97 + (i % 8)) + String((i >> 3) + 1);
  }

  const fileOf = (sq) => sq % BOARD;
  const rankOf = (sq) => sq >> 3;
  const uciOf = (m) => m.from + m.to + (m.promotion || "");

  function _checkChess(chess) {
    if (!chess || typeof chess.moves !== "function" || typeof chess.fen !== "function") {
      throw new TypeError("expected a chess.js (0.10.3) instance");
    }
  }

  const PROMO_ORDER = Object.freeze({ q: 0, r: 1, b: 2, n: 3 });

  /**
   * Sort key copying python-chess generate_legal_moves order (as [category, ...]).
   * Not in check: piece moves (from-square descending, then to-square descending),
   * castling (king side first), pawn captures (from desc, to desc, Q R B N), single
   * pushes (to desc), double pushes (to desc), en passant (from desc). In check
   * (generate_evasions): king moves first (to desc), then the same order without the king.
   */
  function _pyKey(m, inCheck) {
    const from = sqIndex(m.from);
    const to = sqIndex(m.to);
    const pr = m.promotion ? PROMO_ORDER[m.promotion] : 0;
    const castle = m.flags.includes("k") || m.flags.includes("q");
    if (castle) return [2, m.flags.includes("k") ? 0 : 1];
    if (m.piece === "k" && inCheck) return [0, -to];
    if (m.piece !== "p") return [1, -from, -to];
    if (m.flags.includes("e")) return [6, -from];
    if (m.flags.includes("c")) return [3, -from, -to, pr];
    if (m.flags.includes("b")) return [5, -to];
    return [4, -to, pr];
  }

  function _cmpKeys(a, b) {
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      const x = a[i] === undefined ? -Infinity : a[i];
      const y = b[i] === undefined ? -Infinity : b[i];
      if (x !== y) return x < y ? -1 : 1;
    }
    return 0;
  }

  /**
   * chess.moves({verbose: true}) in python-chess legal-move order, so dict-order
   * tie-breaks (max() over candidates, float sums) resolve as in the Python.
   */
  function pyLegalMoves(chess) {
    _checkChess(chess);
    const inCheck = chess.in_check();
    return chess.moves({ verbose: true })
      .map((m) => [_pyKey(m, inCheck), m])
      .sort((a, b) => _cmpKeys(a[0], b[0]))
      .map(([, m]) => m);
  }

  /**
   * Repetition key like python-chess _transposition_key: placement, side to move,
   * castling rights, and the en-passant square only when an en-passant capture is legal.
   * (chess.js 0.10.3 keeps the en-passant square after every double push, so its own
   * in_threefold_repetition() misses repetitions that python-chess counts.)
   */
  function positionKey(chess) {
    _checkChess(chess);
    const [placement, turn, castling, ep] = chess.fen().split(" ");
    let e = "-";
    if (ep !== "-" && chess.moves({ verbose: true }).some((m) => m.flags.includes("e"))) e = ep;
    return `${placement} ${turn} ${castling} ${e}`;
  }

  /**
   * game.py game_outcome: checkmate, insufficient material, stalemate (board.outcome()
   * order), then a threefold repetition that has happened, then the 50-move rule.
   * `seen` maps positionKey -> times reached in this game (the current position included).
   * Returns null while the game goes on, else {termination, winner} with winner 'w'|'b'|null.
   */
  function gameOutcome(chess, seen) {
    _checkChess(chess);
    if (chess.in_checkmate()) return { termination: "checkmate", winner: chess.turn() === "w" ? "b" : "w" };
    if (chess.insufficient_material()) return { termination: "insufficient_material", winner: null };
    if (chess.in_stalemate()) return { termination: "stalemate", winner: null };
    const n = seen instanceof Map ? seen.get(positionKey(chess)) || 0 : 0;
    if (n >= 3) return { termination: "threefold_repetition", winner: null };
    if (parseInt(chess.fen().split(" ")[4], 10) >= 100) return { termination: "fifty_moves", winner: null };
    return null;
  }

  // ---------------------------------------------------------------------------------
  // Straight cuts (split.py)

  function _checkCut(cut) {
    if (!cut || !AXES.includes(cut.axis)) throw new RangeError(`axis must be 'file' or 'rank', got ${cut && cut.axis}`);
    if (!Number.isInteger(cut.after) || cut.after < 0 || cut.after > BOARD - 2) {
      throw new RangeError(`after must be in 0..${BOARD - 2}, got ${cut.after}`);
    }
  }

  function cutCoord(cut, sq) {
    checkSquare(sq);
    return cut.axis === "file" ? fileOf(sq) : rankOf(sq);
  }

  /** Group (0/1) of a square under a cut: 0 when its file (rank) is <= cut.after. */
  function cutSide(cut, sq) {
    return cutCoord(cut, sq) <= cut.after ? 0 : 1;
  }

  function _keys(squares) {
    if (squares instanceof Map) return [...squares.keys()];
    if (squares == null || typeof squares[Symbol.iterator] !== "function") {
      throw new TypeError("squares must be a Map, Set or array of square indices");
    }
    return [...squares];
  }

  /** Every straight cut leaving both groups non-empty; file cuts first, lower `after` first. */
  function cutOptions(squares) {
    const sqs = [...new Set(_keys(squares))];
    for (const sq of sqs) checkSquare(sq);
    const out = [];
    for (const axis of AXES) {
      for (let after = 0; after < BOARD - 1; after++) {
        const cut = { axis, after };
        let has0 = false;
        let has1 = false;
        for (const sq of sqs) {
          if (cutSide(cut, sq) === 0) has0 = true;
          else has1 = true;
        }
        if (has0 && has1) out.push(cut);
      }
    }
    return out;
  }

  /** Number of empty files (or ranks) strictly between the two groups of `cut`. */
  function cutGap(cut, squares) {
    _checkCut(cut);
    const sqs = _keys(squares);
    let low = -Infinity;
    let high = Infinity;
    for (const sq of sqs) {
      const c = cutCoord(cut, sq);
      if (cutSide(cut, sq) === 0) low = Math.max(low, c);
      else high = Math.min(high, c);
    }
    return high - low - 1;
  }

  /** |w0 - w1| / total for the two groups of `cut`. */
  function imbalance(cut, weights) {
    _checkCut(cut);
    const total = pySum(weights.values());
    const w0 = pySum([...weights].filter(([sq]) => cutSide(cut, sq) === 0).map(([, w]) => w));
    return Math.abs(w0 - (total - w0)) / total;
  }

  function _toSquareWeights(weights) {
    if (weights instanceof Map) return weights;
    if (Array.isArray(weights)) return new Map(weights);
    throw new TypeError("weights must be a Map<square, weight> (or an array of [square, weight] pairs)");
  }

  function _checkWeights(weights) {
    if (weights.size < 2) throw new RangeError(`need at least 2 squares to split, got ${weights.size}`);
    for (const [sq, w] of weights) {
      checkSquare(sq);
      if (typeof w !== "number") throw new TypeError(`weight of square ${sq} must be a number, got ${typeof w}`);
      if (!Number.isFinite(w) || !(w > 0)) throw new RangeError(`weight of square ${sq} must be positive and finite, got ${w}`);
    }
  }

  function _checkSplitOpts(balanceTol, minGap) {
    if (typeof balanceTol !== "number" || !Number.isFinite(balanceTol) || balanceTol < 0) {
      throw new RangeError(`balanceTol must be a finite number >= 0, got ${balanceTol}`);
    }
    if (typeof minGap !== "number" || Number.isNaN(minGap) || minGap < 0) {
      throw new RangeError(`minGap must be >= 0, got ${minGap}`);
    }
  }

  /** The cut `split` uses (split.py best_cut). */
  function bestCut(weights, { balanceTol = 0.15, minGap = 1 } = {}) {
    weights = _toSquareWeights(weights);
    _checkWeights(weights);
    _checkSplitOpts(balanceTol, minGap);
    const cuts = cutOptions(weights); // never empty: two distinct squares differ in file or rank
    const scored = cuts.map((c) => [imbalance(c, weights), c]);
    let best = Infinity;
    for (const [s] of scored) if (s < best) best = s;
    const ok = scored.filter(([s]) => s <= best + balanceTol + EPS);
    // min() by key (-min(gap, minGap), round(s, 9)); the first of equal keys wins
    let pick = null;
    let pk0 = 0;
    let pk1 = 0;
    for (const [s, c] of ok) {
      const k0 = -Math.min(cutGap(c, weights), minGap);
      const k1 = pyRound9(s);
      if (pick === null || k0 < pk0 || (k0 === pk0 && k1 < pk1)) {
        pick = c;
        pk0 = k0;
        pk1 = k1;
      }
    }
    return { axis: pick.axis, after: pick.after };
  }

  /**
   * Split weighted squares into [group0, group1] (Sets) by one straight cut.
   * The cut minimises |w0 - w1| / total. Among cuts within balanceTol of the best
   * imbalance, the one with the most empty files/ranks between the groups wins (gaps
   * beyond minGap count as minGap), then the smaller imbalance (rounded to 9 decimals),
   * then file cuts before rank cuts and the lower `after`.
   */
  function split(weights, { balanceTol = 0.15, minGap = 1 } = {}) {
    weights = _toSquareWeights(weights);
    const cut = bestCut(weights, { balanceTol, minGap });
    const g0 = new Set();
    const g1 = new Set();
    for (const sq of weights.keys()) (cutSide(cut, sq) === 0 ? g0 : g1).add(sq);
    return [g0, g1];
  }

  // ---------------------------------------------------------------------------------
  // MoveSelector (split.py)

  function _toMoveWeights(weights) {
    if (weights == null) return new Map();
    if (weights instanceof Map) return weights;
    if (typeof weights === "object" && !Array.isArray(weights)) return new Map(Object.entries(weights));
    if (Array.isArray(weights)) return new Map(weights);
    throw new TypeError("weights must be a Map<uci, weight>");
  }

  /** Weight for every legal move (in legal-move order): given ones, a floor for missing/zero ones. */
  function _resolveWeights(legal, weights, fen) {
    if (legal.length === 0) throw new RangeError("the board has no legal moves (game over)");
    const legalSet = new Set(legal);
    const given = new Map();
    for (const [move, w] of weights) {
      if (!legalSet.has(move)) throw new RangeError(`weight given for ${move} which is not legal on ${fen}`);
      if (typeof w !== "number") throw new TypeError(`weight of ${move} must be a number, got ${typeof w}`);
      if (!Number.isFinite(w) || w < 0) throw new RangeError(`weight of ${move} must be >= 0 and finite, got ${w}`);
      if (w > 0) given.set(move, w);
    }
    const total = pySum(given.values());
    const floor = total > 0 ? FLOOR_SHARE * total : 1.0;
    const out = new Map();
    for (const m of legal) out.set(m, given.has(m) ? given.get(m) : floor);
    return out;
  }

  class MoveSelector {
    /**
     * One move chosen by binary picks: piece, destination, promotion piece, confirm.
     * Feed each decoded pick to choose(); draw groups(), panels(), arrow() and prompt()
     * after each. `move` (a UCI string) is set once stage === 'done'.
     */
    constructor(chess, weights, { confirm = true, balanceTol = 0.15, minGap = 1, undo = false } = {}) {
      _checkChess(chess);
      _checkSplitOpts(balanceTol, minGap);
      this.fen = chess.fen();
      this._info = new Map();
      for (const m of pyLegalMoves(chess)) {
        const uci = uciOf(m);
        this._info.set(uci, {
          from: sqIndex(m.from),
          to: sqIndex(m.to),
          promotion: m.promotion || null,
          san: m.san,
        });
      }
      this.weights = _resolveWeights([...this._info.keys()], _toMoveWeights(weights), this.fen);
      this.confirm = Boolean(confirm);
      this.balanceTol = balanceTol;
      this.minGap = minGap;
      this.undoTarget = Boolean(undo); // c-VEP: a third target, Back, undoes the last pick
      this.picks = 0;
      this.undos = 0;
      this._restart();
    }

    // -- state -------------------------------------------------------------------------

    _restart() {
      this._moves = [...this.weights.keys()];
      this._origin = null;
      this._dest = null;
      this._groups = null;
      this._promo = null;
      this.stage = "piece";
      this.move = null;
      this._history = []; // the state before each pick, for undo()
      this._settle();
    }

    _settle() {
      for (;;) {
        if (this.stage === "piece") {
          const origins = this._squareWeights("from");
          if (origins.size > 1) {
            this._groups = split(origins, { balanceTol: this.balanceTol, minGap: this.minGap });
            return;
          }
          this._origin = origins.keys().next().value;
          this.stage = "dest";
        } else if (this.stage === "dest") {
          const dests = this._squareWeights("to");
          if (dests.size > 1) {
            this._groups = split(dests, { balanceTol: this.balanceTol, minGap: this.minGap });
            return;
          }
          this._dest = dests.keys().next().value;
          this._groups = null;
          this.stage = "promo";
        } else if (this.stage === "promo") {
          if (this._moves.length > 1) {
            this._promo = this._promoSplit();
            return;
          }
          this._promo = null;
          this.stage = this.confirm ? "confirm" : "done";
        } else if (this.stage === "confirm") {
          return;
        } else {
          this.move = this._moves[0];
          return;
        }
      }
    }

    _squareWeights(key) {
      const out = new Map();
      for (const m of this._moves) {
        const sq = this._info.get(m)[key];
        out.set(sq, (out.has(sq) ? out.get(sq) : 0.0) + this.weights.get(m));
      }
      return out;
    }

    _promoSplit() {
      const pt = (m) => {
        const p = this._info.get(m).promotion;
        return p ? PIECE_TYPE[p] : 0;
      };
      // heaviest first, then the stronger promotion; Array.prototype.sort is stable
      const order = [...this._moves].sort((a, b) => {
        const wa = -this.weights.get(a);
        const wb = -this.weights.get(b);
        if (wa !== wb) return wa < wb ? -1 : 1;
        return -pt(a) - -pt(b);
      });
      const ws = order.map((m) => this.weights.get(m));
      const total = pySum(ws);
      let bestK = 1;
      let best = Infinity;
      for (let k = 1; k < order.length; k++) {
        const diff = Math.abs(2 * pySum(ws.slice(0, k)) - total);
        if (diff < best - EPS) {
          bestK = k;
          best = diff;
        }
      }
      return [order.slice(0, bestK), order.slice(bestK)];
    }

    // -- picks -------------------------------------------------------------------------

    /** Apply a decoded pick: keep the group the user looked at (0 or 1). */
    choose(target) {
      if (typeof target !== "number") throw new TypeError(`target must be 0 or 1, got ${typeof target}`);
      if (target !== 0 && target !== 1) throw new RangeError(`target must be 0 or 1, got ${target}`);
      if (this.stage === "done") throw new Error("the move is already chosen; call back() to choose again");
      this.picks += 1;
      if (this.stage === "confirm" && target === 1) {
        this._restart();
        return;
      }
      this._history.push([[...this._moves], this._origin, this._dest, this._groups, this._promo, this.stage]);
      if (this.stage === "piece" || this.stage === "dest") {
        const keep = this._groups[target];
        const key = this.stage === "piece" ? "from" : "to";
        this._moves = this._moves.filter((m) => keep.has(this._info.get(m)[key]));
      } else if (this.stage === "promo") {
        const keep = new Set(this._promo[target]);
        this._moves = this._moves.filter((m) => keep.has(m));
      } else {
        // confirm, target 0: play it
        this.stage = "done";
      }
      this._settle();
    }

    /** Start the move over from the piece stage; `picks` keeps counting. */
    back() {
      this._restart();
    }

    /** True when there is a pick to take back (not at a move's first pick, nor once chosen). */
    get canUndo() {
      return this._history.length > 0 && this.stage !== "done";
    }

    /**
     * The Back target (split.py undo): take back the last pick, back to the screen before
     * it. Counts as a pick; returns false, changing nothing else, when there is nothing to
     * undo (the move's first pick, or right after RESTART).
     */
    undo() {
      if (this.stage === "done") throw new Error("the move is already chosen; there is nothing to undo");
      this.picks += 1;
      if (this._history.length === 0) return false;
      const [moves, origin, dest, groups, promo, stage] = this._history.pop();
      Object.assign(this, { _moves: moves, _origin: origin, _dest: dest, _groups: groups, _promo: promo, stage });
      this.move = null;
      this.undos += 1;
      return true;
    }

    // -- what to draw ------------------------------------------------------------------

    /** The single remaining move in confirm/done, else null. */
    get shownMove() {
      return this.stage === "confirm" || this.stage === "done" ? this._moves[0] : null;
    }

    /** Square of the chosen piece once the piece stage is over, else null. */
    get origin() {
      return this._origin;
    }

    /** Destination square once the dest stage is over, else null. */
    get dest() {
      return this._dest;
    }

    /** 64 chars: '.', '0', '1' or 's' (selected) per square, index = square (a1 first). */
    groups() {
      const chars = new Array(BOARD * BOARD).fill(".");
      if (this.stage === "piece" || this.stage === "dest") {
        this._groups.forEach((group, target) => {
          for (const sq of group) chars[sq] = String(target);
        });
      }
      if (this._origin !== null) chars[this._origin] = "s";
      if (this._dest !== null) chars[this._dest] = "s";
      return chars.join("");
    }

    /** [label0, label1] for the two side panels, or null when the board itself flickers. */
    panels() {
      if (this.stage === "confirm") return this.undoTarget ? ["PLAY", "RESTART"] : ["PLAY", "BACK"];
      if (this.stage === "promo") {
        return this._promo.map((grp) => grp.map((m) => PROMO_NAMES[this._info.get(m).promotion]).join(" / "));
      }
      return null;
    }

    /** The two promotion groups as UCI arrays at the promo stage, else null. */
    promoGroups() {
      return this.stage === "promo" ? this._promo.map((g) => [...g]) : null;
    }

    /** [from, to] squares of the move shown in confirm/done, else null. */
    arrow() {
      const m = this.shownMove;
      if (m === null) return null;
      const i = this._info.get(m);
      return [i.from, i.to];
    }

    prompt() {
      if (this.stage === "piece") return "Look at the piece you want to move";
      if (this.stage === "dest") return "Look at its destination";
      if (this.stage === "promo") return "Promote to?";
      const san = this._info.get(this._moves[0]).san;
      if (this.stage === "confirm") return this.undoTarget ? `Play ${san}? Look at PLAY` : `Play ${san}? Look at PLAY or BACK`;
      return `Playing ${san}`;
    }

    /** SAN of a legal move in this position. */
    san(uci) {
      const i = this._info.get(uci);
      if (!i) throw new RangeError(`${uci} is not legal on ${this.fen}`);
      return i.san;
    }

    /** Candidate squares -> weight in piece/dest stage; the remaining moves (UCI array) otherwise. */
    candidates() {
      if (this.stage === "piece") return this._squareWeights("from");
      if (this.stage === "dest") return this._squareWeights("to");
      return [...this._moves];
    }

    /** Every move still reachable in this attempt, with its weight. */
    candidateMoves() {
      const out = new Map();
      for (const m of this._moves) out.set(m, this.weights.get(m));
      return out;
    }

    /** Whether `uci` can still be chosen without going back. */
    reachable(uci) {
      return this._moves.includes(uci);
    }

    // -- for the simulated user --------------------------------------------------------

    /** Group (0/1) of `square` at the current piece/dest pick, null if not a candidate. */
    sideOf(square) {
      checkSquare(square);
      if (this.stage !== "piece" && this.stage !== "dest") return null;
      for (let t = 0; t < 2; t++) if (this._groups[t].has(square)) return t;
      return null;
    }

    /** The target a user who wants `uci` should look at now. */
    targetFor(uci) {
      if (this.stage === "done") throw new Error("the move is already chosen");
      if (this.stage === "confirm") return uci === this._moves[0] ? 0 : 1;
      if (!this._moves.includes(uci)) {
        throw new Error(`${uci} is no longer reachable at the ${this.stage} stage; wait for confirm and pick BACK`);
      }
      const i = this._info.get(uci);
      if (this.stage === "piece") return this.sideOf(i.from);
      if (this.stage === "dest") return this.sideOf(i.to);
      return this._promo[0].includes(uci) ? 0 : 1;
    }
  }

  /**
   * What game.py's SimulatedUser looks at: the group holding `intended`; once a wrong pick
   * has ruled it out, the group of the heaviest move still reachable; BACK at confirm.
   */
  function userTarget(selector, intended) {
    if (selector.stage === "confirm") return selector.targetFor(intended);
    return selector.targetFor(userMove(selector, intended));
  }

  /**
   * The move game.py's SimulatedUser aims at now: `intended` while reachable, else
   * max(candidate_moves) by weight, ties to the first in python-chess legal order.
   */
  function userMove(selector, intended) {
    if (selector.stage === "confirm" || selector.stage === "done" || selector.reachable(intended)) return intended;
    let move = null;
    let bestW = -Infinity;
    for (const [m, w] of selector.candidateMoves()) {
      if (w > bestW) {
        bestW = w;
        move = m;
      }
    }
    return move;
  }

  /** The c-VEP Back target's index (target 0 and 1 are the groups). */
  const BACK = 2;

  /**
   * What game.py's SimulatedUser looks at with a Back target (c-VEP): Back as soon as a
   * wrong pick has ruled `intended` out, and on the PLAY screen when the wrong move is
   * shown (RESTART when there is nothing to undo); otherwise as userTarget().
   */
  function userLook(selector, intended) {
    if (selector.undoTarget && selector.canUndo && !selector.reachable(intended)) return BACK;
    if (selector.undoTarget && selector.stage === "confirm" && selector.shownMove !== intended) {
      return selector.canUndo ? BACK : 1;
    }
    return userTarget(selector, intended);
  }

  /** Expected picks to choose a move drawn from the (normalised, floored) weights. Exact. */
  function expectedPicks(chess, weights, { confirm = true, balanceTol = 0.15, minGap = 1 } = {}) {
    const opts = { confirm, balanceTol, minGap };
    const resolved = new MoveSelector(chess, weights, opts).weights;
    const total = pySum(resolved.values());
    let out = 0.0;
    for (const [move, w] of resolved) {
      const sel = new MoveSelector(chess, weights, opts);
      while (sel.stage !== "done") sel.choose(sel.targetFor(move));
      if (sel.move !== move) throw new Error(`selector chose ${sel.move} instead of ${move}`);
      out += (w / total) * sel.picks;
    }
    return out;
  }

  // ---------------------------------------------------------------------------------
  // Heuristic prior and opponent (prior.py)

  /** 64-array of {type, color} | null from a chess.js instance (index = python-chess square). */
  function _board64(chess) {
    const rows = chess.board(); // rows[0] is rank 8
    const out = new Array(64).fill(null);
    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        const p = rows[r][f];
        if (p) out[(7 - r) * 8 + f] = { type: p.type, color: p.color };
      }
    }
    return out;
  }

  const KNIGHT_D = [[1, 2], [2, 1], [2, -1], [1, -2], [-1, -2], [-2, -1], [-2, 1], [-1, 2]];
  const KING_D = [[1, 0], [1, 1], [0, 1], [-1, 1], [-1, 0], [-1, -1], [0, -1], [1, -1]];
  const DIAG = [[1, 1], [1, -1], [-1, 1], [-1, -1]];
  const ORTHO = [[1, 0], [-1, 0], [0, 1], [0, -1]];

  /** Squares of `color` pieces attacking `sq` (python-chess Board.attackers; pins ignored). */
  function _attackers(b, color, sq) {
    const f0 = fileOf(sq);
    const r0 = rankOf(sq);
    const out = [];
    const at = (f, r) => (f >= 0 && f < 8 && r >= 0 && r < 8 ? r * 8 + f : -1);
    const is = (s, types) => s >= 0 && b[s] && b[s].color === color && types.includes(b[s].type);
    const pr = color === "w" ? r0 - 1 : r0 + 1; // a pawn attacking sq stands one rank behind it
    for (const df of [-1, 1]) {
      const s = at(f0 + df, pr);
      if (is(s, "p")) out.push(s);
    }
    for (const [df, dr] of KNIGHT_D) {
      const s = at(f0 + df, r0 + dr);
      if (is(s, "n")) out.push(s);
    }
    for (const [df, dr] of KING_D) {
      const s = at(f0 + df, r0 + dr);
      if (is(s, "k")) out.push(s);
    }
    for (const [dirs, types] of [[DIAG, "bq"], [ORTHO, "rq"]]) {
      for (const [df, dr] of dirs) {
        let f = f0 + df;
        let r = r0 + dr;
        while (f >= 0 && f < 8 && r >= 0 && r < 8) {
          const s = r * 8 + f;
          if (b[s]) {
            if (is(s, types)) out.push(s);
            break;
          }
          f += df;
          r += dr;
        }
      }
    }
    return out;
  }

  /** The most material `side` stands to lose to one capture by the other side. */
  function _worstLoss(b, side) {
    const other = side === "w" ? "b" : "w";
    let worst = 0;
    for (const type of ["p", "n", "b", "r", "q"]) {
      const value = PIECE_VALUES[type];
      for (let sq = 0; sq < 64; sq++) {
        const p = b[sq];
        if (!p || p.type !== type || p.color !== side) continue;
        const attackers = _attackers(b, other, sq);
        if (attackers.length === 0) continue;
        if (_attackers(b, side, sq).length === 0) {
          worst = Math.max(worst, value);
        } else {
          let cheapest = Infinity;
          for (const a of attackers) cheapest = Math.min(cheapest, PIECE_VALUES[b[a].type]);
          worst = Math.max(worst, value - cheapest);
        }
      }
    }
    return worst;
  }

  function _centreDistance(sq) {
    return Math.max(Math.abs(fileOf(sq) - 3.5), Math.abs(rankOf(sq) - 3.5));
  }

  /**
   * A centipawn-like score per legal move (Map<uci, number>), from the side to move's view:
   * material won, promotion gain, check, castling, minor-piece development in the opening,
   * steps towards the centre, worst loss from leaving a piece en prise; mate in one scores
   * MATE_SCORE. Deterministic; the chess instance is left as it was (move/undo).
   */
  function heuristicScores(chess) {
    _checkChess(chess);
    const fenParts = chess.fen().split(" ");
    const us = fenParts[1];
    const fullmove = parseInt(fenParts[5], 10);
    const before = _board64(chess);
    const queensOn = before.some((p) => p && p.type === "q");
    const opening = fullmove <= OPENING_MOVES;
    const homeRank = us === "w" ? 0 : 7;
    const out = new Map();
    for (const m of pyLegalMoves(chess)) {
      const uci = uciOf(m);
      const from = sqIndex(m.from);
      const to = sqIndex(m.to);
      const mover = m.piece;
      const gained = m.flags.includes("e") ? PIECE_VALUES.p : m.captured ? PIECE_VALUES[m.captured] : 0;
      const castling = m.flags.includes("k") || m.flags.includes("q");
      const played = chess.move({ from: m.from, to: m.to, promotion: m.promotion });
      if (played === null) throw new Error(`chess.js refused its own move ${uci}`);
      try {
        if (chess.in_checkmate()) {
          out.set(uci, MATE_SCORE);
          continue;
        }
        let score = gained;
        if (m.promotion) score += PIECE_VALUES[m.promotion] - PIECE_VALUES.p;
        if (chess.in_stalemate()) score -= STALEMATE_PENALTY;
        if (chess.in_check()) score += CHECK_BONUS;
        if (castling) score += CASTLE_BONUS;
        else if (mover === "k" && queensOn) score -= EARLY_KING_PENALTY;
        if (mover !== "k" && mover !== "q") {
          score += CENTRE_CP * (_centreDistance(from) - _centreDistance(to));
        }
        if (opening) {
          if ((mover === "n" || mover === "b") && rankOf(from) === homeRank) score += DEVELOP_BONUS;
          if (mover === "q") score -= EARLY_QUEEN_PENALTY;
        }
        score -= _worstLoss(_board64(chess), us);
        out.set(uci, score);
      } finally {
        chess.undo();
      }
    }
    return out;
  }

  /** Scores to a prior: (1 - uniformMix) * softmax(scores / temperature) + uniformMix / n, normalised. */
  function toWeights(scores, { temperatureCp = 100.0, uniformMix = 0.3 } = {}) {
    scores = _toMoveWeights(scores);
    if (scores.size === 0) throw new RangeError("toWeights needs at least one move");
    if (typeof temperatureCp !== "number" || !(temperatureCp > 0) || !Number.isFinite(temperatureCp)) {
      throw new RangeError(`temperatureCp must be a positive finite number, got ${temperatureCp}`);
    }
    if (typeof uniformMix !== "number" || !(uniformMix >= 0 && uniformMix <= 1)) {
      throw new RangeError(`uniformMix must be in [0, 1], got ${uniformMix}`);
    }
    const moves = [...scores.keys()];
    const values = moves.map((m) => scores.get(m));
    if (!values.every((v) => typeof v === "number" && Number.isFinite(v))) {
      throw new RangeError("toWeights needs finite scores");
    }
    let max = -Infinity;
    for (const v of values) if (v > max) max = v;
    const z = values.map((v) => Math.exp((v - max) / temperatureCp));
    const zs = npSum(z);
    const n = moves.length;
    const w = z.map((x) => ((1.0 - uniformMix) * x) / zs + uniformMix / n);
    const ws = npSum(w);
    const out = new Map();
    moves.forEach((m, i) => out.set(m, w[i] / ws));
    return out;
  }

  /** Seeded RNG: () => float in [0, 1). */
  function mulberry32(seed) {
    if (typeof seed !== "number" || !Number.isFinite(seed)) throw new TypeError(`seed must be a finite number, got ${seed}`);
    let a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /** Draw one key of a Map<key, weight> with probability proportional to its weight. */
  function sampleWeighted(weights, rng) {
    let total = 0;
    for (const w of weights.values()) total += w;
    let u = rng() * total;
    let last = null;
    for (const [k, w] of weights) {
      last = k;
      if (u < w) return k;
      u -= w;
    }
    return last;
  }

  /** The heuristic opponent's move (prior.py Engine.reply without Stockfish), as UCI. */
  function opponentReply(chess, rng) {
    _checkChess(chess);
    if (typeof rng !== "function") throw new TypeError("rng must be a function returning floats in [0, 1)");
    const scores = heuristicScores(chess);
    if (scores.size === 0) throw new RangeError("the game is over; there is no move to play");
    const w = toWeights(scores, { temperatureCp: REPLY_TEMPERATURE_CP, uniformMix: REPLY_UNIFORM_MIX });
    return sampleWeighted(w, rng);
  }

  // ---------------------------------------------------------------------------------
  // Simulated decoder + MoveGate (gate.py semantics, in seconds instead of samples)

  const TIME_EPS = 1e-9;

  function _prob(name, p) {
    if (typeof p !== "number" || !(p >= 0 && p <= 1)) throw new RangeError(`${name} must be a probability in [0, 1], got ${p}`);
  }

  class MockDecoder {
    /**
     * Sliding windows of `window` s every `step` s; window k (k >= 1) ends at k * step and
     * exists once k * step >= window. A window is FRESH when it started at least
     * `cooldown` s after the last screen change:
     *     k * step - window >= switchT + cooldown - 1e-9
     * and stale windows are ignored (counted in .stale). For each window, raw is drawn
     * from the gaze: gaze g in {0,1} -> g with pCorrect, 1-g with pWrong, else ABSTAIN;
     * gaze null -> a random target with pStray, else ABSTAIN. `votes` consecutive fresh
     * windows naming the same target fire a pick, which counts as a screen change at that
     * window's end. So after screenChanged(t0) a perfect decoder fires at
     *     (ceil((t0 + cooldown + window) / step - 1e-9) + votes - 1) * step,
     * i.e. exactly t0 + 0.5 + 1.5 + 3 * 0.25 = t0 + 2.75 s when t0 is on the step grid.
     */
    constructor({ step = 0.25, window = 1.5, votes = 4, cooldown = 0.5, pCorrect = 0.75, pWrong = 0.05, pStray = 0.1, rng } = {}) {
      for (const [name, v] of [["step", step], ["window", window]]) {
        if (typeof v !== "number" || !Number.isFinite(v) || !(v > 0)) throw new RangeError(`${name} must be a positive finite number, got ${v}`);
      }
      if (!Number.isInteger(votes) || votes < 1) throw new RangeError(`votes must be a positive integer, got ${votes}`);
      if (typeof cooldown !== "number" || !Number.isFinite(cooldown) || cooldown < 0) {
        throw new RangeError(`cooldown must be finite and >= 0, got ${cooldown}`);
      }
      _prob("pCorrect", pCorrect);
      _prob("pWrong", pWrong);
      _prob("pStray", pStray);
      if (pCorrect + pWrong > 1 + 1e-12) throw new RangeError(`pCorrect + pWrong must be <= 1, got ${pCorrect + pWrong}`);
      if (rng === undefined) rng = mulberry32(0);
      if (typeof rng !== "function") throw new TypeError("rng must be a function returning floats in [0, 1)");
      Object.assign(this, { step, window, votes, cooldown, pCorrect, pWrong, pStray, rng });
      this.lastT = 0; // windows ending in (lastT, t] are processed by the next advance(t)
      this.stale = 0; // windows ignored because they started before the screen settled
      this.fired = 0; // picks fired
      this.windows = 0; // windows processed
      this._k = 1; // index of the next window to process
      this.screenChanged(0);
    }

    /** Stream time at which the current screen appeared. */
    get switchT() {
      return this._switch;
    }

    /** Earliest window start that counts: the screen change plus the settle delay. */
    get freshFrom() {
      return this._switch + this.cooldown;
    }

    /** Target the current run of fresh windows names, or ABSTAIN. */
    get pending() {
      return this._pending;
    }

    /** Length of that run (0 while pending is ABSTAIN); a pick fires at `votes`. */
    get streak() {
      return this._streak;
    }

    /** End time of the first window that can count after the current screen change. */
    get firstFreshEnd() {
      const k = Math.max(1, Math.ceil((this.freshFrom + this.window) / this.step - TIME_EPS));
      return k * this.step;
    }

    /** Earliest possible pick time for the current screen (perfect decoder). */
    get earliestPick() {
      return this.firstFreshEnd + (this.votes - 1) * this.step;
    }

    /** The screen changed at time `t` (default: lastT). Resets the run of agreeing windows. */
    screenChanged(t) {
      const at = t === undefined ? this.lastT : t;
      if (typeof at !== "number" || !Number.isFinite(at)) throw new RangeError(`screenChanged time must be finite, got ${at}`);
      this._switch = at;
      this._pending = ABSTAIN;
      this._streak = 0;
    }

    _raw(g) {
      if (g === null) {
        if (this.rng() < this.pStray) return this.rng() < 0.5 ? 0 : 1;
        return ABSTAIN;
      }
      const u = this.rng();
      if (u < this.pCorrect) return g;
      if (u < this.pCorrect + this.pWrong) return 1 - g;
      return ABSTAIN;
    }

    /**
     * Process the windows ending in (lastT, t] with the user looking at `gaze` (0, 1, null,
     * or a function (windowEnd) => 0|1|null). Returns one event per window:
     * {t, raw, fresh, pending, streak, fired}. `fired` is 0|1 on the window that fires a pick
     * (its pending/streak show the full run, i.e. streak === votes) and null otherwise.
     * Processing STOPS at a fired pick (lastT becomes that window's end) so the caller can
     * apply the pick and redraw before later windows are decoded with the new gaze; call
     * advance(t, ...) again to continue.
     */
    advance(t, gaze) {
      if (typeof t !== "number" || !Number.isFinite(t)) throw new RangeError(`advance time must be finite, got ${t}`);
      const gazeFn = typeof gaze === "function" ? gaze : null;
      const check = (g) => {
        if (g === undefined) g = null;
        if (g !== null && g !== 0 && g !== 1) throw new RangeError(`gaze must be 0, 1 or null, got ${g}`);
        return g;
      };
      const fixed = gazeFn ? null : check(gaze);
      const events = [];
      if (t <= this.lastT) return events;
      for (;;) {
        const end = this._k * this.step;
        if (end > t + TIME_EPS) break;
        this._k += 1;
        if (end - this.window < -TIME_EPS) continue; // window not full yet
        this.windows += 1;
        const g = gazeFn ? check(gazeFn(end)) : fixed;
        const ev = this.push(end, this._raw(g));
        events.push(ev);
        if (ev.fired !== null) {
          this.lastT = end;
          return events;
        }
      }
      this.lastT = t;
      return events;
    }

    /**
     * The MoveGate step on its own (gate.py MoveGate.push): one decoded window ending at
     * time `end` with prediction `raw` (0, 1 or ABSTAIN). Returns its event. advance()
     * calls this; it is public so the gate can be tested with chosen predictions.
     */
    push(end, raw) {
      if (raw !== 0 && raw !== 1 && raw !== ABSTAIN) throw new RangeError(`raw must be 0, 1 or ABSTAIN, got ${raw}`);
      const fresh = end - this.window >= this.freshFrom - TIME_EPS;
      if (!fresh) {
        this.stale += 1;
        return { t: end, raw, fresh, pending: this._pending, streak: this._streak, fired: null };
      }
      if (raw === ABSTAIN) {
        this._pending = ABSTAIN;
        this._streak = 0;
      } else if (raw === this._pending) {
        this._streak += 1;
      } else {
        this._pending = raw;
        this._streak = 1;
      }
      if (this._pending !== ABSTAIN && this._streak >= this.votes) {
        const target = this._pending;
        const ev = { t: end, raw, fresh, pending: target, streak: this._streak, fired: target };
        this.fired += 1;
        this.screenChanged(end);
        return ev;
      }
      return { t: end, raw, fresh, pending: this._pending, streak: this._streak, fired: null };
    }
  }

  // ---------------------------------------------------------------------------------
  // Simulated c-VEP decoder: gate.py CodeGate + brainsync.vep.live.CodeVepDecoder

  function _gauss(rng) {
    let u = 0;
    while (u === 0) u = rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rng());
  }

  class MockCodeDecoder {
    /**
     * One decision per code cycle, as CodeVepDecoder behind CodeGate: every screen restarts
     * the code, cycle k of it begins at switchT + k * cycle, and a cycle counts only if it
     * began `cooldown` s after the change (so with the defaults the first is dropped). Its
     * decision comes `pad` s after the cycle ends, from the mean of the last `cycles`
     * counted cycles: a score per target (a simulated correlation), raw = the best target
     * (ABSTAIN while the best mean stays under `floor`), lead = best - runner-up. `votes`
     * agreeing decisions fire a pick, which is a screen change at that decision. Per cycle
     * the gazed target's response shows with pCorrect, another target's with pWrong, none
     * otherwise; looking at nothing (gaze null) shows a random target's with pStray. So a
     * perfect decoder fires (ceil(cooldown / cycle) + votes) * cycle + pad after a change:
     * 3 * 1.05 + 0.5 = 3.65 s with the defaults.
     */
    constructor({ cycle = 63 / 60, cycles = 2, votes = 2, cooldown = 0.5, pad = 0.5, nTargets = 3,
      pCorrect = 0.9, pWrong = 0.05, pStray = 0.1, signal = 0.6, noise = 0.06, floor = 0.25, rng } = {}) {
      for (const [name, v] of [["cycle", cycle], ["signal", signal]]) {
        if (typeof v !== "number" || !Number.isFinite(v) || !(v > 0)) throw new RangeError(`${name} must be a positive finite number, got ${v}`);
      }
      for (const [name, v] of [["cycles", cycles], ["votes", votes], ["nTargets", nTargets]]) {
        if (!Number.isInteger(v) || v < 1) throw new RangeError(`${name} must be a positive integer, got ${v}`);
      }
      for (const [name, v] of [["cooldown", cooldown], ["pad", pad], ["noise", noise], ["floor", floor]]) {
        if (typeof v !== "number" || !Number.isFinite(v) || v < 0) throw new RangeError(`${name} must be finite and >= 0, got ${v}`);
      }
      _prob("pCorrect", pCorrect);
      _prob("pWrong", pWrong);
      _prob("pStray", pStray);
      if (pCorrect + pWrong > 1 + 1e-12) throw new RangeError(`pCorrect + pWrong must be <= 1, got ${pCorrect + pWrong}`);
      if (rng === undefined) rng = mulberry32(0);
      if (typeof rng !== "function") throw new TypeError("rng must be a function returning floats in [0, 1)");
      Object.assign(this, { cycle, cycles, votes, cooldown, pad, nTargets, pCorrect, pWrong, pStray, signal, noise, floor, rng });
      this.lastT = 0;
      this.stale = 0; // cycles dropped because they began before the screen settled
      this.fired = 0;
      this.windows = 0; // decisions made
      this.last = null; // the last decision event (for the confidence display)
      this.screenChanged(0);
    }

    get switchT() {
      return this._switch;
    }

    get freshFrom() {
      return this._switch + this.cooldown;
    }

    get pending() {
      return this._pending;
    }

    get streak() {
      return this._streak;
    }

    get committed() {
      return this._committed;
    }

    /** Index (in this screen) of the first cycle that counts. */
    get firstCycle() {
      return Math.max(0, Math.ceil(this.cooldown / this.cycle - TIME_EPS));
    }

    /** Time of the first decision that can count after the current screen change. */
    get firstFreshEnd() {
      return this._switch + (this.firstCycle + 1) * this.cycle + this.pad;
    }

    /** Earliest possible pick time for the current screen (perfect decoder). */
    get earliestPick() {
      return this.firstFreshEnd + (this.votes - 1) * this.cycle;
    }

    /** The screen changed at `t` (default: lastT): the code restarts, cycles and votes reset. */
    screenChanged(t) {
      const at = t === undefined ? this.lastT : t;
      if (typeof at !== "number" || !Number.isFinite(at)) throw new RangeError(`screenChanged time must be finite, got ${at}`);
      this._switch = at;
      this._k = 0;
      this._recent = [];
      this._pending = ABSTAIN;
      this._streak = 0;
      this._committed = ABSTAIN;
    }

    /** One cycle's simulated scores with the user looking at `g` (a target or null). */
    _cycleScores(g) {
      let hot = null;
      const u = this.rng();
      if (g === null) {
        if (u < this.pStray) hot = Math.floor(this.rng() * this.nTargets);
      } else if (u < this.pCorrect) {
        hot = g;
      } else if (u < this.pCorrect + this.pWrong) {
        hot = (g + 1 + Math.floor(this.rng() * (this.nTargets - 1))) % this.nTargets;
      }
      const out = [];
      for (let k = 0; k < this.nTargets; k++) out.push((k === hot ? this.signal : 0) + this.noise * _gauss(this.rng));
      return out;
    }

    /**
     * Decide every cycle whose decision time falls in (lastT, t], with the user looking at
     * `gaze` (a target, null, or a function (decisionTime) => target|null). Returns one
     * event per cycle: {t, raw, fresh, scores, lead, pending, streak, committed, changed,
     * fired}; a dropped cycle comes back with fresh false and is not counted. Stops at a
     * fired pick (lastT becomes its time) so the caller can redraw first.
     */
    advance(t, gaze) {
      if (typeof t !== "number" || !Number.isFinite(t)) throw new RangeError(`advance time must be finite, got ${t}`);
      const gazeFn = typeof gaze === "function" ? gaze : null;
      const check = (g) => {
        if (g === undefined) g = null;
        if (g !== null && !(Number.isInteger(g) && g >= 0 && g < this.nTargets)) {
          throw new RangeError(`gaze must be 0..${this.nTargets - 1} or null, got ${g}`);
        }
        return g;
      };
      const fixed = gazeFn ? null : check(gaze);
      const events = [];
      if (t <= this.lastT) return events;
      for (;;) {
        const begin = this._switch + this._k * this.cycle;
        const at = begin + this.cycle + this.pad;
        if (at > t + TIME_EPS) break;
        this._k += 1;
        const g = gazeFn ? check(gazeFn(at)) : fixed;
        const cyc = this._cycleScores(g);
        if (begin < this.freshFrom - TIME_EPS) {
          this.stale += 1;
          events.push({ t: at, raw: ABSTAIN, fresh: false, scores: cyc, lead: null, pending: this._pending,
            streak: this._streak, committed: this._committed, changed: false, fired: null });
          continue;
        }
        const ev = this.push(at, cyc);
        events.push(ev);
        if (ev.fired !== null) {
          this.lastT = at;
          return events;
        }
      }
      this.lastT = t;
      return events;
    }

    /** One counted cycle's scores decided at time `at` (public for tests). */
    push(at, cycleScores) {
      if (!Array.isArray(cycleScores) || cycleScores.length !== this.nTargets) {
        throw new RangeError(`cycleScores must hold ${this.nTargets} numbers`);
      }
      this._recent.push(cycleScores.slice());
      while (this._recent.length > this.cycles) this._recent.shift();
      this.windows += 1;
      const scores = [];
      for (let k = 0; k < this.nTargets; k++) {
        scores.push(this._recent.reduce((a, c) => a + c[k], 0) / this._recent.length);
      }
      const order = scores.map((s, k) => [s, k]).sort((a, b) => b[0] - a[0] || a[1] - b[1]);
      const raw = order[0][0] >= this.floor ? order[0][1] : ABSTAIN;
      const lead = order.length > 1 ? order[0][0] - order[1][0] : order[0][0];
      if (raw === ABSTAIN) {
        this._pending = ABSTAIN;
        this._streak = 0;
      } else if (raw === this._pending) {
        this._streak += 1;
      } else {
        this._pending = raw;
        this._streak = 1;
      }
      let fired = null;
      let changed = false;
      if (this._pending !== ABSTAIN && this._streak >= this.votes && this._committed !== this._pending) {
        fired = this._pending;
        changed = true;
        this._committed = fired;
      }
      const ev = { t: at, raw, fresh: true, scores, lead, pending: this._pending, streak: this._streak,
        committed: this._committed, changed, fired };
      this.last = ev;
      if (fired !== null) {
        this.fired += 1;
        this.screenChanged(at);
      }
      return ev;
    }
  }

  const api = Object.freeze({
    ABSTAIN,
    BACK,
    BOARD,
    STAGES,
    FLOOR_SHARE,
    PROMO_NAMES,
    PIECE_VALUES,
    MATE_SCORE,
    REPLY_TEMPERATURE_CP,
    REPLY_UNIFORM_MIX,
    sqIndex,
    sqName,
    uciOf,
    cutOptions,
    cutSide,
    cutGap,
    imbalance,
    bestCut,
    split,
    MoveSelector,
    userTarget,
    userMove,
    userLook,
    pyLegalMoves,
    positionKey,
    gameOutcome,
    expectedPicks,
    heuristicScores,
    toWeights,
    mulberry32,
    sampleWeighted,
    opponentReply,
    MockDecoder,
    MockCodeDecoder,
    _internal: Object.freeze({ pySum, npSum, pyRound9, attackers: _attackers, board64: _board64 }),
  });
  root.BrainChessCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
