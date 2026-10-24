// Tests for brainsync/web/logic.js (the BrainSwipe page's pure helpers).
// Run: node --test tests/web/
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import * as L from "../../brainsync/web/logic.js";

const WEB = new URL("../../brainsync/web/", import.meta.url);

describe("constants", () => {
  test("targets match brainsync.swipe (Pass left 0, Like right 1)", () => {
    assert.equal(L.PASS, 0);
    assert.equal(L.LIKE, 1);
    assert.equal(L.ABSTAIN, -1);
    assert.deepEqual([...L.SIDES], ["pass", "like"]);
    assert.deepEqual([...L.GAZES], ["auto", "card", "pass", "like"]);
  });

  test("defaults follow the contract", () => {
    assert.deepEqual(L.DEFAULT_STATE.labels, ["Pass", "Like"]);
    assert.deepEqual(L.DEFAULT_STATE.freqs, [15.0, 17.0]);
    assert.deepEqual(L.DEFAULT_STATE.colors, ["#d0605a", "#5aae6e"]);
    assert.equal(L.DEFAULT_STATE.gaze, "card");
  });
});

describe("normalizeState", () => {
  test("null and garbage give the defaults", () => {
    for (const raw of [null, undefined, 3, "x", []]) {
      const s = L.normalizeState(raw);
      assert.equal(s.name, "BrainSwipe");
      assert.equal(s.decoder, true);
      assert.deepEqual(s.labels, ["Pass", "Like"]);
      assert.equal(s.n_cards, 0);
      assert.deepEqual(s.deck, []);
      assert.deepEqual(s.counts, { swipes: 0, likes: 0, passes: 0, matches: 0, stale: 0 });
    }
  });

  test("a full contract payload passes through", () => {
    const raw = {
      name: "BrainSwipe", decoder: false, labels: ["Nope", "Yes"], freqs: [15, 17.5],
      colors: ["#112233", "#445566"], votes: 5, window_s: 2, step_s: 0.5, cooldown_s: 0.75,
      switch_s: 6, gaze: "like", card: 3, n_cards: 4,
      deck: [{ name: "A", age: 30, detail: "d", color: "#abcdef" }, { name: "B", age: 22, color: "#000000" }],
      counts: { swipes: 7, likes: 3, passes: 4, matches: 1, stale: 56 }, started: 1700000000.5,
    };
    const s = L.normalizeState(raw);
    assert.equal(s.decoder, false);
    assert.deepEqual(s.labels, ["Nope", "Yes"]);
    assert.deepEqual(s.freqs, [15, 17.5]);
    assert.deepEqual(s.colors, ["#112233", "#445566"]);
    assert.equal(s.votes, 5);
    assert.equal(s.cooldown_s, 0.75);
    assert.equal(s.gaze, "like");
    assert.equal(s.card, 3);
    assert.equal(s.n_cards, 4);
    assert.equal(s.deck[1].detail, "");
    assert.deepEqual(s.counts, raw.counts);
    assert.equal(s.started, 1700000000.5);
  });

  test("bad fields fall back one by one", () => {
    const s = L.normalizeState({
      labels: ["only one"], freqs: ["x", 17], colors: ["red", "#5aae6e"], votes: 0,
      gaze: "sideways", card: -2, counts: { swipes: -1, likes: "2", matches: NaN },
    });
    assert.deepEqual(s.labels, ["Pass", "Like"]);
    assert.deepEqual(s.freqs, [15, 17]);
    assert.deepEqual(s.colors, ["#d0605a", "#5aae6e"]);
    assert.equal(s.votes, 1);
    assert.equal(s.gaze, "card");
    assert.equal(s.card, 0);
    assert.deepEqual(s.counts, { swipes: 0, likes: 2, passes: 0, matches: 0, stale: 0 });
  });

  test("n_cards defaults to the deck length", () => {
    const s = L.normalizeState({ deck: [{ name: "A", age: 1, color: "#000000" }] });
    assert.equal(s.n_cards, 1);
  });

  test("returned arrays are copies, not the frozen defaults", () => {
    const s = L.normalizeState(null);
    s.labels[0] = "changed";
    assert.equal(L.DEFAULT_STATE.labels[0], "Pass");
  });
});

describe("colours", () => {
  test("isHexColor", () => {
    assert.ok(L.isHexColor("#d0605a"));
    assert.ok(L.isHexColor("#ABCDEF"));
    assert.ok(!L.isHexColor("#abc"));
    assert.ok(!L.isHexColor("d0605a"));
    assert.ok(!L.isHexColor(null));
  });

  test("hexToRgba", () => {
    assert.equal(L.hexToRgba("#d0605a", 0.25), "rgba(208, 96, 90, 0.25)");
    assert.equal(L.hexToRgba("#5aae6e", 1), "rgba(90, 174, 110, 1)");
    assert.equal(L.hexToRgba("nope", 0.5), "rgba(128, 128, 128, 0.5)");
  });
});

describe("progress", () => {
  test("streak over votes", () => {
    assert.equal(L.progress(0, 4), 0);
    assert.equal(L.progress(1, 4), 0.25);
    assert.equal(L.progress(3, 4), 0.75);
    assert.equal(L.progress(4, 4), 1);
  });

  test("clamped and safe", () => {
    assert.equal(L.progress(9, 4), 1);
    assert.equal(L.progress(-1, 4), 0);
    assert.equal(L.progress(2, 0), 0);
    assert.equal(L.progress(NaN, 4), 0);
    assert.equal(L.progress(2, undefined), 0);
  });
});

describe("squareStates", () => {
  const win = (o) => ({ t: 1, scores: [0, 0], raw: -1, pending: -1, streak: 0, votes: 4,
                        stale: false, gaze: "card", true: -1, ...o });

  test("no window: both empty", () => {
    for (const s of L.squareStates(null, 4)) {
      assert.deepEqual(s, { fill: 0, steps: 0, settling: false, cue: false });
    }
  });

  test("pending target fills its own square only", () => {
    const [p, l] = L.squareStates(win({ pending: 1, raw: 1, streak: 3 }), 4);
    assert.equal(p.fill, 0);
    assert.equal(l.fill, 0.75);
    assert.equal(l.steps, 3);
    assert.equal(l.cue, true);
    assert.equal(p.cue, false);
  });

  test("the cue box appears at CUE_STREAK fresh windows", () => {
    assert.equal(L.CUE_STREAK, 2);
    assert.equal(L.squareStates(win({ pending: 0, streak: 1 }), 4)[0].cue, false);
    assert.equal(L.squareStates(win({ pending: 0, streak: 2 }), 4)[0].cue, true);
  });

  test("stale windows: both settling, no progress", () => {
    const st = L.squareStates(win({ stale: true, pending: 1, streak: 3 }), 4);
    for (const s of st) {
      assert.equal(s.settling, true);
      assert.equal(s.fill, 0);
      assert.equal(s.cue, false);
    }
  });

  test("abstain: nothing pending", () => {
    const st = L.squareStates(win({ pending: -1, streak: 0 }), 4);
    assert.equal(st[0].fill + st[1].fill, 0);
  });

  test("streak is clamped to votes and votes falls back", () => {
    const [, l] = L.squareStates(win({ pending: 1, streak: 9, votes: undefined }), 4);
    assert.equal(l.steps, 4);
    assert.equal(l.fill, 1);
  });
});

describe("gaze from input", () => {
  test("gazeFromKeys: latest pressed arrow wins", () => {
    assert.equal(L.gazeFromKeys([]), null);
    assert.equal(L.gazeFromKeys(undefined), null);
    assert.equal(L.gazeFromKeys(["ArrowLeft"]), "pass");
    assert.equal(L.gazeFromKeys(["ArrowRight"]), "like");
    assert.equal(L.gazeFromKeys(["ArrowLeft", "ArrowRight"]), "like");
    assert.equal(L.gazeFromKeys(["ArrowRight", "ArrowLeft"]), "pass");
    assert.equal(L.gazeFromKeys(new Set(["ArrowRight"])), "like");
    assert.equal(L.gazeFromKeys(["Shift", "a"]), null);
    assert.equal(L.gazeFromKeys(["ArrowLeft", "Shift"]), "pass");
  });

  test("nextGazeOnPointer: hover in and out", () => {
    assert.equal(L.nextGazeOnPointer("pointerenter", "pass", null), "pass");
    assert.equal(L.nextGazeOnPointer("pointerleave", "pass", "pass"), null);
    assert.equal(L.nextGazeOnPointer("pointerenter", "like", "pass"), "like");
    // leaving a square the pointer was no longer on changes nothing
    assert.equal(L.nextGazeOnPointer("pointerleave", "pass", "like"), "like");
  });

  test("nextGazeOnPointer: touch press and hold", () => {
    assert.equal(L.nextGazeOnPointer("pointerdown", "like", null), "like");
    assert.equal(L.nextGazeOnPointer("pointerup", "like", "like"), null);
    assert.equal(L.nextGazeOnPointer("pointercancel", "like", "like"), null);
  });

  test("nextGazeOnPointer: a move over a square looks at it", () => {
    // focus left and came back with the mouse resting on Like: no pointerenter comes,
    // so the next move must restore the gaze
    assert.equal(L.nextGazeOnPointer("pointermove", "like", null), "like");
    assert.equal(L.nextGazeOnPointer("pointermove", "like", "like"), "like");
    assert.equal(L.nextGazeOnPointer("pointermove", "like", "pass"), "like");
  });

  test("app.js restores hover on pointermove and on focus", () => {
    const app = readFileSync(new URL("app.js", WEB), "utf8");
    assert.match(app, /"pointerenter", "pointermove"/);
    assert.match(app, /addEventListener\("focus", onFocus\)/);
  });

  test("nextGazeOnPointer: other events and sides are ignored", () => {
    assert.equal(L.nextGazeOnPointer("pointerover", "like", "pass"), "pass");
    assert.equal(L.nextGazeOnPointer("pointerenter", "middle", "pass"), "pass");
    assert.equal(L.nextGazeOnPointer("pointerenter", "pass"), "pass");
  });

  test("decideGaze: keys beat hover beat auto, else card", () => {
    assert.equal(L.decideGaze(), "card");
    assert.equal(L.decideGaze({}), "card");
    assert.equal(L.decideGaze({ auto: true }), "auto");
    assert.equal(L.decideGaze({ auto: true, hover: "pass" }), "pass");
    assert.equal(L.decideGaze({ hover: "like" }), "like");
    assert.equal(L.decideGaze({ hover: "like", keys: ["ArrowLeft"] }), "pass");
    assert.equal(L.decideGaze({ auto: true, keys: ["ArrowRight"] }), "like");
    assert.equal(L.decideGaze({ hover: "bogus" }), "card");
  });

  test("every decided gaze is a valid API value", () => {
    for (const auto of [false, true]) {
      for (const hover of [null, "pass", "like"]) {
        for (const keys of [[], ["ArrowLeft"], ["ArrowRight", "ArrowLeft"]]) {
          assert.ok(L.GAZES.includes(L.decideGaze({ auto, hover, keys })));
        }
      }
    }
  });

  test("instantSwipeTarget", () => {
    // with the decoder, plain arrows look and Shift+arrows swipe
    assert.equal(L.instantSwipeTarget("ArrowLeft", false, true), null);
    assert.equal(L.instantSwipeTarget("ArrowLeft", true, true), 0);
    assert.equal(L.instantSwipeTarget("ArrowRight", true, true), 1);
    // without it, any arrow swipes
    assert.equal(L.instantSwipeTarget("ArrowLeft", false, false), 0);
    assert.equal(L.instantSwipeTarget("ArrowRight", false, false), 1);
    assert.equal(L.instantSwipeTarget("ArrowUp", true, true), null);
    assert.equal(L.instantSwipeTarget("a", false, false), null);
  });
});

describe("card motion", () => {
  test("cardSlideDirection", () => {
    assert.equal(L.cardSlideDirection(0), "left");
    assert.equal(L.cardSlideDirection(1), "right");
    assert.equal(L.cardSlideDirection("1"), "right");
    assert.equal(L.cardSlideDirection(-1), "left");
  });

  test("slideClasses: slide, or a crossfade with reduced motion", () => {
    assert.deepEqual(L.slideClasses(0, false), { out: "card--out-left", in: "card--in" });
    assert.deepEqual(L.slideClasses(1, false), { out: "card--out-right", in: "card--in" });
    assert.deepEqual(L.slideClasses(1, true), { out: "card--fade-out", in: "card--fade-in" });
  });

  test("leavingClasses: a card still arriving drops its entry class", () => {
    for (const reduced of [false, true]) {
      for (const t of [0, 1]) {
        const ch = L.leavingClasses(t, reduced);
        const cls = L.slideClasses(t, reduced);
        assert.deepEqual(ch.add, ["is-leaving", cls.out]);
        // whichever way the card came in, its entry animation must not win
        for (const c of L.CARD_IN_CLASSES) assert.ok(ch.remove.includes(c), c);
        assert.ok(ch.remove.includes(L.slideClasses(t, !reduced).in));
      }
    }
  });

  test("leaving rules come after entry rules in style.css", () => {
    // same specificity, so the later rule wins if both classes are ever on one card
    const css = readFileSync(new URL("style.css", WEB), "utf8");
    const at = (c) => css.search(new RegExp(`^\\.${c}\\s*\\{`, "m"));
    for (const out of ["card--out-left", "card--out-right", "card--fade-out"]) {
      for (const inn of ["card--in", "card--fade-in"]) {
        assert.ok(at(inn) >= 0 && at(out) > at(inn), `${out} must follow ${inn}`);
      }
    }
  });

  test("the classes exist in style.css", () => {
    const css = readFileSync(new URL("style.css", WEB), "utf8");
    for (const c of ["card--out-left", "card--out-right", "card--in", "card--fade-out", "card--fade-in"]) {
      assert.ok(css.includes(`.${c}`), c);
    }
  });
});

describe("rates and counts", () => {
  test("swipesPerMinute: intervals since the first swipe", () => {
    assert.equal(L.swipesPerMinute(0, null, 10), null);
    assert.equal(L.swipesPerMinute(1, 100, 103), null);       // nothing to measure yet
    assert.equal(L.swipesPerMinute(2, 100, 103), 20);         // one swipe 3 s later
    assert.equal(L.swipesPerMinute(19, 100, 154), 20);        // 18 more in 54 s
    assert.equal(L.swipesPerMinute(2, 100, 100), null);       // no time passed
    assert.equal(L.swipesPerMinute(2, 100, 99), null);        // clock went backwards
  });

  test("swipesPerMinute: page opened mid-run", () => {
    // the first swipe this page saw was number 10
    assert.equal(L.swipesPerMinute(10, 50, 60, 10), null);
    assert.equal(L.swipesPerMinute(12, 50, 56, 10), 20);
  });

  test("serverRestarted: a new start time or fewer swipes", () => {
    const seen = { started: 1000.5, counts: { swipes: 8 } };
    assert.equal(L.serverRestarted(seen, { started: 1000.5, counts: { swipes: 8 } }), false);
    assert.equal(L.serverRestarted(seen, { started: 1000.5, counts: { swipes: 9 } }), false);
    assert.equal(L.serverRestarted(seen, { started: 1600, counts: { swipes: 0 } }), true);
    assert.equal(L.serverRestarted(seen, { started: 1000.5, counts: { swipes: 0 } }), true);
    // restarted and already swiped past the old count: the start time still tells
    assert.equal(L.serverRestarted(seen, { started: 1600, counts: { swipes: 12 } }), true);
    // the page's defaults before any state (started 0) are not a restart
    assert.equal(L.serverRestarted({ started: 0, counts: { swipes: 0 } },
      { started: 1600, counts: { swipes: 3 } }), false);
    assert.equal(L.serverRestarted(null, seen), false);
  });

  test("swipesPerMinute: bad input", () => {
    assert.equal(L.swipesPerMinute(5, NaN, 10), null);
    assert.equal(L.swipesPerMinute(5, 1, undefined), null);
  });

  test("formatRate", () => {
    assert.equal(L.formatRate(null), "–");
    assert.equal(L.formatRate(NaN), "–");
    assert.equal(L.formatRate(20), "20.0");
    assert.equal(L.formatRate(18.149), "18.1");
  });

  test("formatCounts: order, labels, values", () => {
    const items = L.formatCounts({ swipes: 18, likes: 10, passes: 8, matches: 4, stale: 144 }, 18.1);
    assert.deepEqual(items.map((i) => i.key), ["swipes", "rate", "likes", "passes", "matches", "stale"]);
    assert.deepEqual(items.map((i) => i.value), ["18", "18.1", "10", "8", "4", "144"]);
    assert.ok(items.every((i) => typeof i.label === "string" && i.label.length > 0));
  });

  test("formatCounts: missing counts read 0", () => {
    const items = L.formatCounts(undefined);
    assert.deepEqual(items.map((i) => i.value), ["0", "–", "0", "0", "0", "0"]);
  });

  test("normalizeCounts", () => {
    assert.deepEqual(L.normalizeCounts({ swipes: 2.7, extra: 1 }),
      { swipes: 2, likes: 0, passes: 0, matches: 0, stale: 0 });
  });
});

describe("score readout", () => {
  test("bars share one scale with a floor", () => {
    assert.deepEqual(L.scoreBars([2, 1]), [0.5, 0.25]);        // floor 4
    assert.deepEqual(L.scoreBars([8, 2]), [1, 0.25]);          // scale 8
    assert.deepEqual(L.scoreBars([-8, 2]), [0, 0.25]);         // negatives draw nothing
    assert.deepEqual(L.scoreBars([1, 1], 2), [0.5, 0.5]);
  });

  test("bars tolerate junk", () => {
    assert.deepEqual(L.scoreBars(undefined), [0, 0]);
    assert.deepEqual(L.scoreBars([3]), [0.75, 0]);
    assert.deepEqual(L.scoreBars(["x", null]), [0, 0]);
  });

  test("targetLabel", () => {
    assert.equal(L.targetLabel(0), "Pass");
    assert.equal(L.targetLabel(1), "Like");
    assert.equal(L.targetLabel(-1), "card");
    assert.equal(L.targetLabel(-1, ["P", "L"], "nothing"), "nothing");
    assert.equal(L.targetLabel(1, ["P", "L"]), "L");
  });

  test("gazeLabel", () => {
    assert.equal(L.gazeLabel("pass"), "looking at Pass");
    assert.equal(L.gazeLabel("like"), "looking at Like");
    assert.equal(L.gazeLabel("auto"), "auto schedule");
    assert.equal(L.gazeLabel("card"), "reading the card");
    assert.equal(L.gazeLabel(undefined), "reading the card");
  });
});

describe("swipes", () => {
  const swipe = (n, o = {}) => ({
    target: 1, label: "Like", source: "decoder", swiped: 0, card: 1, liked: true, match: false,
    voice: "Liked Priya", counts: { swipes: n, likes: n, passes: 0, matches: 0, stale: 8 * n }, ...o,
  });

  test("isNewSwipe dedupes the POST reply and the broadcast", () => {
    assert.equal(L.isNewSwipe(0, swipe(1)), true);
    assert.equal(L.isNewSwipe(1, swipe(1)), false);
    assert.equal(L.isNewSwipe(1, swipe(2)), true);
    assert.equal(L.isNewSwipe(5, swipe(2)), false);
    assert.equal(L.isNewSwipe(0, null), false);
    assert.equal(L.isNewSwipe(0, { counts: {} }), false);
  });

  test("swipeAnnouncement names the next profile", () => {
    const deck = [{ name: "Priya", age: 29 }, { name: "Marcus", age: 31 }];
    assert.equal(L.swipeAnnouncement(swipe(1), deck), "Liked Priya. Next: Marcus, 31.");
    assert.equal(L.swipeAnnouncement(swipe(1, { voice: "Pass", liked: false }), deck),
      "Pass. Next: Marcus, 31.");
    assert.equal(
      L.swipeAnnouncement(swipe(1, { voice: "Priya liked you back. It's a match!" }), deck),
      "Priya liked you back. It's a match! Next: Marcus, 31.");
    assert.equal(L.swipeAnnouncement(swipe(1, { card: 7 }), deck), "Liked Priya.");
    assert.equal(L.swipeAnnouncement(null, deck), "");
  });

  test("cardAlt and cardUrl", () => {
    assert.equal(L.cardAlt({ name: "Priya", age: 29, detail: "Product designer · Oakland" }),
      "Profile card: Priya, 29. Product designer · Oakland");
    assert.equal(L.cardAlt({ name: "Marcus", age: 31, detail: "" }), "Profile card: Marcus, 31");
    assert.equal(L.cardAlt(undefined), "Profile card");
    assert.equal(L.cardUrl(0), "/cards/0.png");
    assert.equal(L.cardUrl(12), "/cards/12.png");
    assert.equal(L.cardUrl("3"), "/cards/3.png");
    assert.equal(L.cardUrl(-4), "/cards/0.png");
    assert.equal(L.cardUrl("../x"), "/cards/0.png");
  });
});

describe("reconnectDelay", () => {
  test("doubles from 0.5 s to a 5 s cap", () => {
    assert.deepEqual([0, 1, 2, 3, 4, 5, 50].map(L.reconnectDelay), [500, 1000, 2000, 4000, 5000, 5000, 5000]);
    assert.equal(L.reconnectDelay(-3), 500);
    assert.equal(L.reconnectDelay(undefined), 500);
  });
});

describe("clampLayout", () => {
  const opts = { top: 76, bottom: 132 };

  test("desktop 1440x900: wide, stimulus proportions", () => {
    const lay = L.clampLayout(1440, 900, opts);
    assert.equal(lay.mode, "wide");
    const unit = lay.stageH - 32;   // squares 0.4 units, centres at least one unit apart
    assert.ok(Math.abs(lay.square / unit - 0.4) < 0.01, `${lay.square} / ${unit}`);
    assert.ok(lay.pitch >= unit, `${lay.pitch} < ${unit}`);
    // squares and labels fit within the width
    assert.ok(lay.pitch + lay.square <= 1440 - 32);
    // the card fits between the squares' inner edges, with a margin, and is 2:3
    assert.ok(lay.cardW <= lay.pitch - lay.square - 2 * 20);
    assert.ok(Math.abs(lay.cardH / lay.cardW - 1.5) < 0.02);
    assert.ok(lay.cardH <= 900 - opts.top - opts.bottom);
    assert.ok(lay.cardH >= 450, `card ${lay.cardH} px tall is too small at 1440x900`);
  });

  test("wide layouts never overflow horizontally", () => {
    for (const [w, h] of [[1920, 1080], [1280, 800], [1024, 768], [800, 600], [760, 700], [2560, 1440], [1440, 600]]) {
      const lay = L.clampLayout(w, h, opts);
      assert.equal(lay.mode, "wide", `${w}x${h}`);
      assert.ok(lay.pitch + lay.square <= w - 32, `${w}x${h}: ${JSON.stringify(lay)}`);
      assert.ok(lay.cardW + 2 * 20 <= lay.pitch - lay.square, `${w}x${h}: card overlaps squares`);
      assert.ok(lay.cardH <= h - opts.top - opts.bottom || lay.cardH <= 200, `${w}x${h}: card too tall`);
    }
  });

  test("laptop and projector windows: the card uses the stage height", () => {
    // at pitch = 1 unit the card was 185x278 at 1366x650 and 120x180 at 1024x600
    for (const [w, h, bottom] of [[1366, 650, 132], [1024, 600, 132], [1024, 600, 170],
                                  [1280, 720, 132], [1440, 900, 132], [1920, 1080, 132]]) {
      const lay = L.clampLayout(w, h, { top: 76, bottom });
      assert.equal(lay.mode, "wide", `${w}x${h}`);
      assert.ok(lay.cardH >= lay.stageH - 8 - 2, `${w}x${h}+${bottom}: ${JSON.stringify(lay)}`);
      assert.ok(lay.pitch + lay.square <= w - 32, `${w}x${h}: wider than the window`);
      assert.ok(lay.cardW + 2 * 20 <= lay.pitch - lay.square, `${w}x${h}: card overlaps squares`);
    }
    assert.ok(L.clampLayout(1366, 650, opts).cardW >= 280);
  });

  test("a narrow desktop window shrinks the stage to its width", () => {
    const narrow = L.clampLayout(1000, 940, opts);   // width-limited: 1.4 units > 968 px
    const roomy = L.clampLayout(1800, 940, opts);
    assert.equal(narrow.mode, "wide");
    assert.ok(narrow.pitch < roomy.pitch);
    assert.ok(narrow.pitch + narrow.square <= 1000 - 32);
  });

  test("phone 390x844: stacked, nothing wider than the screen", () => {
    const lay = L.clampLayout(390, 844, opts);
    assert.equal(lay.mode, "stack");
    assert.ok(lay.cardW <= 390 - 32);
    assert.ok(2 * lay.square + (lay.pitch - lay.square) <= 390 - 32);
    assert.ok(lay.square >= 64);
    assert.ok(Math.abs(lay.cardH / lay.cardW - 1.5) < 0.02);
    // the card and the squares are both on the first screen
    assert.ok(opts.top + lay.cardH + lay.square <= 844, JSON.stringify(lay));
  });

  test("portrait tablets and tiny phones stack too", () => {
    for (const [w, h] of [[768, 1024], [320, 568], [414, 896], [700, 900], [240, 320]]) {
      const lay = L.clampLayout(w, h, opts);
      assert.equal(lay.mode, "stack", `${w}x${h}`);
      assert.ok(lay.cardW <= Math.max(w, 240) - 32 + 1 || lay.cardH === 240, `${w}x${h}: ${JSON.stringify(lay)}`);
      assert.ok(2 * lay.square + (lay.pitch - lay.square) <= Math.max(w, 240) - 32 + 1, `${w}x${h}`);
    }
  });

  test("garbage sizes give a sane layout", () => {
    const lay = L.clampLayout(NaN, undefined);
    assert.ok(["wide", "stack"].includes(lay.mode));
    for (const k of ["square", "pitch", "cardW", "cardH", "stageH"]) {
      assert.ok(Number.isFinite(lay[k]) && lay[k] > 0, k);
    }
  });
});

describe("page files", () => {
  const html = readFileSync(new URL("index.html", WEB), "utf8");
  const app = readFileSync(new URL("app.js", WEB), "utf8");
  const css = readFileSync(new URL("style.css", WEB), "utf8");
  const all = [html, app, css, readFileSync(fileURLToPath(new URL("logic.js", WEB)), "utf8")];

  test("no external resources (the page must work offline)", () => {
    for (const text of all) {
      assert.ok(!/https?:\/\/(?!www\.w3\.org\/2000\/svg)/.test(text), "found an http(s) URL");
      assert.ok(!/@import/.test(text));
    }
  });

  test("the product name is BrainSwipe and the page says it is a mock", () => {
    assert.ok(html.includes("BrainSwipe"));
    assert.ok(html.includes("simulated EEG board, no flicker"));
    // real dating-app names, spelled backwards so this file does not contain them either
    const banned = ["egnih", "rednit", "elbmub"].map((w) => [...w].reverse().join(""));
    for (const text of all) {
      for (const word of banned) assert.ok(!text.toLowerCase().includes(word), "real app name found");
    }
  });

  test("every logic.js helper app.js calls exists", () => {
    // app.js imports the namespace (L.x), so a renamed helper is only undefined at run
    // time; this ties the two files together
    const used = new Set([...app.matchAll(/\bL\.([A-Za-z_$][\w$]*)/g)].map((m) => m[1]));
    assert.ok(used.size >= 20, `only ${used.size} helpers found`);
    const missing = [...used].filter((name) => !(name in L));
    assert.deepEqual(missing, []);
  });

  test("assets load from /static/ and app.js imports logic.js relatively", () => {
    assert.ok(html.includes('src="/static/app.js"'));
    assert.ok(html.includes('href="/static/style.css"'));
    assert.ok(app.includes('from "./logic.js"'));
  });

  test("app.js uses only the contract's endpoints and events", () => {
    const paths = [...app.matchAll(/["'`](\/(?:api|cards)\/[^"'`$]*)/g)].map((m) => m[1]);
    for (const p of paths) {
      assert.ok(["/api/state", "/api/events", "/api/gaze", "/api/swipe",
                 "/api/speech?text="].includes(p), p);
    }
    // the voice is Cartesia through /api/speech; the browser's own voice is never used
    assert.ok(!app.includes("speechSynthesis"));
    for (const ev of ["state", "window", "swipe", "gaze"]) {
      assert.ok(app.includes(`addEventListener("${ev}"`), ev);
    }
  });

  test("no flicker: nothing animates the squares on a loop", () => {
    // the only infinite animation allowed is the connecting spinner on the placeholder
    const infinite = [...css.matchAll(/([^{}]+)\{[^}]*infinite[^}]*\}/g)].map((m) => m[1].trim());
    assert.deepEqual(infinite, [".placeholder__pulse"]);
    assert.ok(!/setInterval\([^)]*(square|flash)/i.test(app));
  });

  test("controls are real buttons with labels", () => {
    for (const id of ["btn-auto", "btn-mute", "label-0", "label-1"]) {
      assert.match(html, new RegExp(`<button[^>]*id="${id}"`), id);
    }
    assert.ok(html.includes('aria-live="polite"'));
    assert.ok(html.includes("✕") || html.includes("&#x2715;"));
    assert.ok(html.includes("♥") || html.includes("&#x2665;"));
  });

  test("colours are custom properties", () => {
    for (const v of ["--bg", "--pass", "--like", "--text"]) assert.ok(css.includes(`${v}:`), v);
    assert.ok(css.includes("#d0605a") && css.includes("#5aae6e"));
  });
});
