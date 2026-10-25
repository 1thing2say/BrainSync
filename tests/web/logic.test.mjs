// Tests for brainsync/web/logic.js (the BrainSwipe page's pure helpers, c-VEP standard).
// Run: node --test tests/web/
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

import * as L from "../../brainsync/web/logic.js";

const WEB = new URL("../../brainsync/web/", import.meta.url);
// the standard's browser helpers (clients/js/cvep.js), to check the texts match it
const C = createRequire(import.meta.url)("../../clients/js/cvep.js");

describe("constants", () => {
  test("targets follow the standard: Pass 0, Super like 1, Like 2", () => {
    assert.deepEqual([L.PASS, L.SUPER, L.LIKE], [0, 1, 2]);
    assert.deepEqual([...L.TARGETS], [0, 1, 2]);
    assert.equal(L.ABSTAIN, -1);
    assert.deepEqual([...L.SIDES], ["pass", "super", "like"]);
    assert.deepEqual([...L.GAZES], ["auto", "card", "pass", "super", "like"]);
    assert.equal(L.BAR_CELLS, C.BAR_CELLS);
  });

  test("defaults follow the server's state", () => {
    const d = L.DEFAULT_STATE;
    assert.deepEqual(d.labels, ["Pass", "Super like", "Like"]);
    assert.deepEqual(d.colors, ["#d0605a", "#5a9bd0", "#5aae6e"]);
    assert.deepEqual(d.lags, [0, C.LAG_FRAMES, 2 * C.LAG_FRAMES]);
    assert.equal(d.training.of, C.TRAIN_CYCLES);
    assert.equal(d.gaze, "card");
    assert.equal(d.paradigm, "code");
  });
});

describe("normalizeState", () => {
  const COUNTS0 = { swipes: 0, likes: 0, super_likes: 0, passes: 0, matches: 0, stale: 0 };

  test("null and garbage give the defaults", () => {
    for (const raw of [null, undefined, 3, "x", []]) {
      const s = L.normalizeState(raw);
      assert.equal(s.name, "BrainSwipe");
      assert.equal(s.decoder, true);
      assert.deepEqual(s.labels, ["Pass", "Super like", "Like"]);
      assert.equal(s.n_cards, 0);
      assert.deepEqual(s.deck, []);
      assert.deepEqual(s.counts, COUNTS0);
      assert.equal(s.training.active, false);
    }
  });

  test("a full server payload passes through", () => {
    const raw = {
      name: "BrainSwipe", decoder: false, paradigm: "code", labels: ["No", "Wow", "Yes"],
      colors: ["#112233", "#445566", "#778899"], lags: [0, 21, 42], cycle_s: 1.05, votes: 3,
      margin: 0.1, cycles: 2,
      training: { active: true, cycle: 12, of: 30, accuracy: null, chance: 0.3333, target: 0 },
      cooldown_s: 0.75, switch_s: 6, gaze: "super", card: 3, n_cards: 4,
      deck: [{ name: "A", age: 30, detail: "d", color: "#abcdef" }, { name: "B", age: 22, color: "#000000" }],
      counts: { swipes: 7, likes: 3, super_likes: 1, passes: 4, matches: 2, stale: 6 },
      started: 1700000000.5,
    };
    const s = L.normalizeState(raw);
    assert.equal(s.decoder, false);
    assert.deepEqual(s.labels, ["No", "Wow", "Yes"]);
    assert.deepEqual(s.colors, raw.colors);
    assert.deepEqual(s.lags, [0, 21, 42]);
    assert.equal(s.votes, 3);
    assert.equal(s.margin, 0.1);
    assert.deepEqual(s.training, raw.training);
    assert.equal(s.cooldown_s, 0.75);
    assert.equal(s.gaze, "super");
    assert.equal(s.card, 3);
    assert.equal(s.deck[1].detail, "");
    assert.deepEqual(s.counts, raw.counts);
    assert.equal(s.started, 1700000000.5);
  });

  test("bad fields fall back one by one", () => {
    const s = L.normalizeState({
      labels: ["only one"], colors: ["red", "#5a9bd0", "#5aae6e"], lags: [0, "x", 42],
      votes: 0, gaze: "sideways", card: -2,
      training: { active: 1, cycle: 99, of: 0, accuracy: "x", target: 7 },
      counts: { swipes: -1, likes: "2", matches: NaN },
    });
    assert.deepEqual(s.labels, ["Pass", "Super like", "Like"]);
    assert.deepEqual(s.colors, ["#d0605a", "#5a9bd0", "#5aae6e"]);
    assert.deepEqual(s.lags, [0, 21, 42]);
    assert.equal(s.votes, 1);
    assert.equal(s.gaze, "card");
    assert.equal(s.card, 0);
    assert.equal(s.training.active, true);
    assert.equal(s.training.of, 1);
    assert.equal(s.training.cycle, 1);          // never past "of"
    assert.equal(s.training.accuracy, null);
    assert.equal(s.training.target, 0);
    assert.deepEqual(s.counts, { ...COUNTS0, likes: 2 });
  });

  test("returned arrays are copies, not the frozen defaults", () => {
    const s = L.normalizeState(null);
    s.labels[0] = "changed";
    assert.equal(L.DEFAULT_STATE.labels[0], "Pass");
  });
});

describe("colours", () => {
  test("isHexColor and hexToRgba", () => {
    assert.ok(L.isHexColor("#d0605a") && L.isHexColor("#ABCDEF"));
    assert.ok(!L.isHexColor("#abc") && !L.isHexColor("d0605a") && !L.isHexColor(null));
    assert.equal(L.hexToRgba("#d0605a", 0.25), "rgba(208, 96, 90, 0.25)");
    assert.equal(L.hexToRgba("nope", 0.5), "rgba(128, 128, 128, 0.5)");
  });
});

describe("confidence (the standard's texts)", () => {
  test("meterLevel and textBar match cvep.js level and bar", () => {
    for (const v of [-0.2, 0, 0.06, 0.42, 0.99, 1.5, NaN, null, "x"]) {
      assert.equal(L.meterLevel(v), C.level(v), String(v));
      assert.equal(L.textBar(v), C.bar(v), String(v));
    }
  });

  test("scoreText", () => {
    assert.equal(L.scoreText(0.42), "+0.42");
    assert.equal(L.scoreText(-0.034), "-0.03");
    assert.equal(L.scoreText(0), "+0.00");
    assert.equal(L.scoreText(null), "–");
    assert.equal(L.scoreText(NaN), "–");
  });

  test("statusText matches cvep.js statusLine", () => {
    const labels = ["Pass", "Super like", "Like"];
    const d = { lead: 0.31, committed: 2, changed: true };
    assert.equal(L.statusText(d, labels), C.statusLine(0.31, "Like", true));
    assert.equal(L.statusText(d, labels), "lead 0.31   choice: Like *");
    const none = { lead: null, committed: -1, changed: false };
    assert.equal(L.statusText(none, labels), C.statusLine(NaN, null, false));
    assert.equal(L.statusText({ lead: 0.05, committed: 0, changed: false }, labels),
      "lead 0.05   choice: Pass");
    assert.equal(L.statusText(null), "no decision yet");
  });

  test("trainingText: the standard's training and template lines", () => {
    const labels = ["Pass", "Super like", "Like"];
    assert.equal(L.trainingText({ active: true, cycle: 12, of: 30, target: 0 }, labels),
      "training: keep looking at Pass   cycle 12/30");
    assert.equal(L.trainingText({ active: false, cycle: 30, of: 30, accuracy: 0.83,
                                  chance: 1 / 3 }, labels),
      "template ready: 83 % on held-out cycles (chance 33 %)");
    assert.equal(L.trainingText({ active: false, accuracy: null }), "");
  });

  test("trainingProgress", () => {
    assert.equal(L.trainingProgress({ active: true, cycle: 15, of: 30 }), 0.5);
    assert.equal(L.trainingProgress({ active: true, cycle: 0, of: 30 }), 0);
    assert.equal(L.trainingProgress({ active: false }), 1);
  });
});

describe("squareStates", () => {
  const dec = (o) => ({ t: 1, scores: [0.1, 0.42, -0.03], raw: 1, committed: -1, lead: 0.32,
                        changed: false, fired: -1, stale: 0, gaze: "super", true: 1, ...o });

  test("no decision: empty bars", () => {
    for (const s of L.squareStates(null)) {
      assert.deepEqual(s, { level: 0, score: null, best: false, committed: false, cue: false,
                            settling: false });
    }
  });

  test("a decision fills every bar with its own score", () => {
    const s = L.squareStates(dec({ committed: 1 }));
    assert.deepEqual(s.map((x) => x.level), [0.1, 0.42, 0]);
    assert.deepEqual(s.map((x) => x.score), [0.1, 0.42, -0.03]);
    assert.deepEqual(s.map((x) => x.best), [false, true, false]);
    assert.deepEqual(s.map((x) => x.committed), [false, true, false]);
  });

  test("training: the cue box on the training target, no bars", () => {
    const s = L.squareStates(dec(), { training: true, target: 0 });
    assert.deepEqual(s.map((x) => x.cue), [true, false, false]);
    assert.ok(s.every((x) => x.level === 0 && !x.best));
  });

  test("settling after a swipe: bars empty, the swiped square keeps its box", () => {
    const s = L.squareStates(dec(), { settling: true, cue: 2 });
    assert.ok(s.every((x) => x.settling && x.level === 0));
    assert.deepEqual(s.map((x) => x.cue), [false, false, true]);
  });

  test("junk scores read as empty", () => {
    const s = L.squareStates(dec({ scores: ["x", null] }));
    assert.deepEqual(s.map((x) => x.level), [0, 0, 0]);
    assert.deepEqual(s.map((x) => x.score), [null, null, null]);
  });
});

describe("gaze from input", () => {
  test("gazeFromKeys: latest pressed arrow wins", () => {
    assert.equal(L.gazeFromKeys([]), null);
    assert.equal(L.gazeFromKeys(undefined), null);
    assert.equal(L.gazeFromKeys(["ArrowLeft"]), "pass");
    assert.equal(L.gazeFromKeys(["ArrowUp"]), "super");
    assert.equal(L.gazeFromKeys(["ArrowRight"]), "like");
    assert.equal(L.gazeFromKeys(["ArrowLeft", "ArrowUp"]), "super");
    assert.equal(L.gazeFromKeys(["ArrowUp", "ArrowLeft"]), "pass");
    assert.equal(L.gazeFromKeys(new Set(["ArrowRight"])), "like");
    assert.equal(L.gazeFromKeys(["Shift", "a", "ArrowDown"]), null);
  });

  test("isGazeKey", () => {
    for (const k of ["ArrowLeft", "ArrowUp", "ArrowRight"]) assert.ok(L.isGazeKey(k), k);
    for (const k of ["ArrowDown", "a", "toString", undefined]) assert.ok(!L.isGazeKey(k), k);
  });

  test("nextGazeOnPointer: hover, touch and moves on all three squares", () => {
    assert.equal(L.nextGazeOnPointer("pointerenter", "super", null), "super");
    assert.equal(L.nextGazeOnPointer("pointerleave", "super", "super"), null);
    assert.equal(L.nextGazeOnPointer("pointerleave", "pass", "like"), "like");
    assert.equal(L.nextGazeOnPointer("pointerdown", "like", null), "like");
    assert.equal(L.nextGazeOnPointer("pointerup", "like", "like"), null);
    assert.equal(L.nextGazeOnPointer("pointermove", "super", "pass"), "super");
    assert.equal(L.nextGazeOnPointer("pointerover", "like", "pass"), "pass");
    assert.equal(L.nextGazeOnPointer("pointerenter", "middle", "pass"), "pass");
  });

  test("app.js restores hover on pointermove and on focus", () => {
    const app = readFileSync(new URL("app.js", WEB), "utf8");
    assert.match(app, /"pointerenter", "pointermove"/);
    assert.match(app, /addEventListener\("focus", onFocus\)/);
  });

  test("decideGaze: keys beat hover beat auto, else card", () => {
    assert.equal(L.decideGaze(), "card");
    assert.equal(L.decideGaze({ auto: true }), "auto");
    assert.equal(L.decideGaze({ auto: true, hover: "super" }), "super");
    assert.equal(L.decideGaze({ hover: "like", keys: ["ArrowUp"] }), "super");
    assert.equal(L.decideGaze({ hover: "bogus" }), "card");
  });

  test("every decided gaze is a valid API value", () => {
    for (const auto of [false, true]) {
      for (const hover of [null, "pass", "super", "like"]) {
        for (const keys of [[], ["ArrowLeft"], ["ArrowRight", "ArrowUp"]]) {
          assert.ok(L.GAZES.includes(L.decideGaze({ auto, hover, keys })));
        }
      }
    }
  });

  test("instantSwipeTarget", () => {
    // with the decoder, plain arrows look and Shift+arrows swipe
    assert.equal(L.instantSwipeTarget("ArrowLeft", false, true), null);
    assert.equal(L.instantSwipeTarget("ArrowLeft", true, true), 0);
    assert.equal(L.instantSwipeTarget("ArrowUp", true, true), 1);
    assert.equal(L.instantSwipeTarget("ArrowRight", true, true), 2);
    // without it, any of the three arrows swipes
    assert.equal(L.instantSwipeTarget("ArrowUp", false, false), 1);
    assert.equal(L.instantSwipeTarget("ArrowDown", true, true), null);
    assert.equal(L.instantSwipeTarget("a", false, false), null);
  });
});

describe("card motion", () => {
  test("cardSlideDirection and slideClasses", () => {
    assert.deepEqual([0, 1, 2, -1, "2"].map(L.cardSlideDirection),
      ["left", "up", "right", "left", "right"]);
    assert.deepEqual(L.slideClasses(1, false), { out: "card--out-up", in: "card--in" });
    assert.deepEqual(L.slideClasses(2, true), { out: "card--fade-out", in: "card--fade-in" });
  });

  test("leavingClasses: a card still arriving drops its entry class", () => {
    for (const reduced of [false, true]) {
      for (const t of [0, 1, 2]) {
        const ch = L.leavingClasses(t, reduced);
        assert.deepEqual(ch.add, ["is-leaving", L.slideClasses(t, reduced).out]);
        for (const c of L.CARD_IN_CLASSES) assert.ok(ch.remove.includes(c), c);
      }
    }
  });

  test("leaving rules come after entry rules in style.css, and all exist", () => {
    const css = readFileSync(new URL("style.css", WEB), "utf8");
    const at = (c) => css.search(new RegExp(`^\\.${c}\\s*\\{`, "m"));
    for (const out of ["card--out-left", "card--out-up", "card--out-right", "card--fade-out"]) {
      for (const inn of ["card--in", "card--fade-in"]) {
        assert.ok(at(inn) >= 0 && at(out) > at(inn), `${out} must follow ${inn}`);
      }
    }
  });
});

describe("rates and counts", () => {
  test("swipesPerMinute", () => {
    assert.equal(L.swipesPerMinute(0, null, 10), null);
    assert.equal(L.swipesPerMinute(1, 100, 103), null);
    assert.equal(L.swipesPerMinute(2, 100, 103), 20);
    assert.equal(L.swipesPerMinute(12, 50, 56, 10), 20);
    assert.equal(L.swipesPerMinute(2, 100, 99), null);
    assert.equal(L.swipesPerMinute(5, NaN, 10), null);
  });

  test("serverRestarted: a new start time or fewer swipes", () => {
    const seen = { started: 1000.5, counts: { swipes: 8 } };
    assert.equal(L.serverRestarted(seen, { started: 1000.5, counts: { swipes: 9 } }), false);
    assert.equal(L.serverRestarted(seen, { started: 1600, counts: { swipes: 12 } }), true);
    assert.equal(L.serverRestarted(seen, { started: 1000.5, counts: { swipes: 0 } }), true);
    assert.equal(L.serverRestarted(null, seen), false);
  });

  test("formatRate and formatCounts", () => {
    assert.equal(L.formatRate(null), "–");
    assert.equal(L.formatRate(18.149), "18.1");
    const items = L.formatCounts({ swipes: 18, likes: 10, super_likes: 3, passes: 8,
                                   matches: 5, stale: 14 }, 18.1);
    assert.deepEqual(items.map((i) => i.key),
      ["swipes", "rate", "likes", "super_likes", "passes", "matches", "stale"]);
    assert.deepEqual(items.map((i) => i.value), ["18", "18.1", "10", "3", "8", "5", "14"]);
    assert.deepEqual(L.formatCounts(undefined).map((i) => i.value),
      ["0", "–", "0", "0", "0", "0", "0"]);
  });

  test("every stat has its element in index.html", () => {
    const html = readFileSync(new URL("index.html", WEB), "utf8");
    for (const item of L.formatCounts({})) assert.ok(html.includes(`id="stat-${item.key}"`), item.key);
  });

  test("targetLabel and gazeLabel", () => {
    assert.equal(L.targetLabel(1), "Super like");
    assert.equal(L.targetLabel(-1), "card");
    assert.equal(L.targetLabel(5, ["a", "b", "c"], "none"), "none");
    assert.equal(L.gazeLabel("super"), "looking at Super like");
    assert.equal(L.gazeLabel("auto"), "auto schedule");
    assert.equal(L.gazeLabel(undefined), "reading the card");
  });
});

describe("swipes", () => {
  const swipe = (n, o = {}) => ({
    target: 2, label: "Like", source: "decoder", swiped: 0, card: 1, liked: true,
    super_like: false, match: false, voice: "Liked Priya",
    counts: { swipes: n, likes: n, super_likes: 0, passes: 0, matches: 0, stale: 2 * n }, ...o,
  });
  const deck = [{ name: "Priya", age: 29 }, { name: "Marcus", age: 31 }];

  test("isNewSwipe dedupes the POST reply and the broadcast", () => {
    assert.equal(L.isNewSwipe(0, swipe(1)), true);
    assert.equal(L.isNewSwipe(1, swipe(1)), false);
    assert.equal(L.isNewSwipe(0, null), false);
  });

  test("swipeAnnouncement names the next profile", () => {
    assert.equal(L.swipeAnnouncement(swipe(1), deck), "Liked Priya. Next: Marcus, 31.");
    assert.equal(
      L.swipeAnnouncement(swipe(1, { voice: "Super liked Priya. It's a match!" }), deck),
      "Super liked Priya. It's a match! Next: Marcus, 31.");
    assert.equal(L.swipeAnnouncement(null, deck), "");
  });

  test("matchBanner: a super like has its own banner", () => {
    assert.equal(L.matchBanner(swipe(1), deck), null);
    assert.deepEqual(L.matchBanner(swipe(1, { match: true }), deck),
      { title: "It’s a match!", line: "Priya liked you back" });
    assert.deepEqual(L.matchBanner(swipe(1, { match: true, super_like: true, target: 1 }), deck),
      { title: "Super like!", line: "Priya is a match" });
    assert.equal(L.matchBanner(swipe(1, { match: true, swiped: 9 }), deck).line,
      "Someone liked you back");
  });

  test("cardAlt and cardUrl", () => {
    assert.equal(L.cardAlt({ name: "Marcus", age: 31, detail: "" }), "Profile card: Marcus, 31");
    assert.equal(L.cardAlt(undefined), "Profile card");
    assert.equal(L.cardUrl(12), "/cards/12.png");
    assert.equal(L.cardUrl("../x"), "/cards/0.png");
  });

  test("reconnectDelay doubles from 0.5 s to a 5 s cap", () => {
    assert.deepEqual([0, 1, 2, 3, 4, 50].map(L.reconnectDelay), [500, 1000, 2000, 4000, 5000, 5000]);
  });
});

describe("clampLayout", () => {
  const opts = { top: 76, bottom: 150 };

  test("wide windows: Super like above the card, Pass and Like beside it", () => {
    for (const [w, h] of [[1440, 900], [1920, 1080], [1280, 800], [1366, 650], [1024, 768],
                          [800, 600], [2560, 1440]]) {
      const lay = L.clampLayout(w, h, opts);
      assert.equal(lay.mode, "wide", `${w}x${h}`);
      const msg = `${w}x${h}: ${JSON.stringify(lay)}`;
      assert.ok(lay.pitch + lay.square <= w - 32, `wider than the window, ${msg}`);
      assert.ok(lay.cardW + 2 * 20 <= lay.pitch - lay.square, `card over a square, ${msg}`);
      assert.ok(lay.superH + lay.cardH <= lay.stageH, `taller than the stage, ${msg}`);
      assert.ok(Math.abs(lay.cardH / lay.cardW - 1.5) < 0.03, msg);
      assert.ok(lay.superH > lay.square, msg);   // room for the label and bar under it
    }
  });

  test("a laptop window keeps a readable card", () => {
    const lay = L.clampLayout(1440, 900, opts);
    assert.ok(lay.cardH >= 400, JSON.stringify(lay));
    assert.ok(lay.square >= 100, JSON.stringify(lay));
  });

  test("a narrow desktop window shrinks the card to the width", () => {
    const narrow = L.clampLayout(800, 760, opts);
    assert.equal(narrow.mode, "wide");
    assert.ok(narrow.pitch + narrow.square <= 800 - 32);
  });

  test("phones stack: card on top, three squares in a row", () => {
    for (const [w, h] of [[390, 844], [320, 568], [768, 1024], [240, 320]]) {
      const lay = L.clampLayout(w, h, opts);
      assert.equal(lay.mode, "stack", `${w}x${h}`);
      const inner = Math.max(w, 240) - 32;
      assert.ok(3 * lay.square + 2 * (lay.pitch - lay.square) <= inner + 1, `${w}x${h}`);
      assert.ok(lay.cardW <= inner + 1, `${w}x${h}: ${JSON.stringify(lay)}`);
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
    const used = new Set([...app.matchAll(/\bL\.([A-Za-z_$][\w$]*)/g)].map((m) => m[1]));
    assert.ok(used.size >= 25, `only ${used.size} helpers found`);
    const missing = [...used].filter((name) => !(name in L));
    assert.deepEqual(missing, []);
  });

  test("every element id app.js asks for is in index.html", () => {
    const ids = new Set([...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));
    const asked = [...app.matchAll(/\$\("([^"]+)"\)/g)].map((m) => m[1]);
    const templated = [...app.matchAll(/\$\(`([a-z-]+)-\$\{[a-z]+\}`\)/g)].map((m) => m[1]);
    assert.ok(asked.length > 15);
    for (const id of asked) assert.ok(ids.has(id), id);
    for (const prefix of templated) {
      for (const t of L.TARGETS) assert.ok(ids.has(`${prefix}-${t}`), `${prefix}-${t}`);
    }
  });

  test("three squares, labels and confidence meters", () => {
    for (const t of L.TARGETS) {
      for (const id of [`square-${t}`, `label-${t}`, `meter-${t}`, `meter-fill-${t}`, `meter-val-${t}`]) {
        assert.ok(html.includes(`id="${id}"`), id);
      }
      assert.match(html, new RegExp(`<button[^>]*id="label-${t}"`));
    }
    for (const side of L.SIDES) assert.ok(html.includes(`data-side="${side}"`), side);
  });

  test("assets load from /static/ and app.js imports logic.js relatively", () => {
    assert.ok(html.includes('src="/static/app.js"'));
    assert.ok(html.includes('href="/static/style.css"'));
    assert.ok(app.includes('from "./logic.js"'));
  });

  test("app.js uses only the server's endpoints and events", () => {
    const paths = [...app.matchAll(/["'`](\/(?:api|cards)\/[^"'`$]*)/g)].map((m) => m[1]);
    for (const p of paths) {
      assert.ok(["/api/state", "/api/events", "/api/gaze", "/api/swipe",
                 "/api/speech?text="].includes(p), p);
    }
    // the voice is Cartesia through /api/speech; the browser's own voice is never used
    assert.ok(!app.includes("speechSynthesis"));
    for (const ev of ["state", "train", "trained", "decision", "swipe", "gaze"]) {
      assert.ok(app.includes(`addEventListener("${ev}"`), ev);
    }
  });

  test("no flicker: the squares are static white and nothing loops on them", () => {
    // the only infinite animation allowed is the connecting spinner on the placeholder
    const infinite = [...css.matchAll(/([^{}]+)\{[^}]*infinite[^}]*\}/g)].map((m) => m[1].trim());
    assert.deepEqual(infinite, [".placeholder__pulse"]);
    assert.ok(!/setInterval\([^)]*(square|flash)/i.test(app));
    assert.ok(!/requestAnimationFrame\([^)]*(square|flicker|frame)/i.test(app));
    assert.match(css, /--square:\s*#ffffff/);
    const square = css.match(/^\.square\s*\{[^}]*\}/m)[0];
    assert.ok(square.includes("background: var(--square)"));
    assert.ok(!/animation/.test(square));
  });

  test("controls are real buttons with labels", () => {
    for (const id of ["btn-auto", "btn-mute"]) {
      assert.match(html, new RegExp(`<button[^>]*id="${id}"`), id);
    }
    assert.ok(html.includes('aria-live="polite"'));
    for (const icon of ["&#x2715;", "&#x2605;", "&#x2665;"]) assert.ok(html.includes(icon), icon);
  });

  test("colours are custom properties", () => {
    for (const v of ["--bg", "--pass", "--super", "--like", "--text", "--square"]) {
      assert.ok(css.includes(`${v}:`), v);
    }
    assert.ok(css.includes("#d0605a") && css.includes("#5a9bd0") && css.includes("#5aae6e"));
  });
});
