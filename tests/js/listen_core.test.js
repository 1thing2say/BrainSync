// ui/listen_core.js with a fake recognizer and a fake clock: run with `node --test tests/js/`
// (tests/test_js.py runs it under pytest). No browser, no network.
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const LC = require("../../ui/listen_core.js");

// A fake clock with setTimeout/clearTimeout driven by advance().
function clock() {
  let t = 0;
  let id = 0;
  const timers = new Map();
  return {
    now: () => t,
    setTimeout(fn, ms) { id += 1; timers.set(id, { at: t + ms, fn }); return id; },
    clearTimeout(i) { timers.delete(i); },
    advance(ms) {
      const end = t + ms;
      for (;;) {
        let next = null;
        for (const [i, x] of timers) if (x.at <= end && (!next || x.at < next[1].at)) next = [i, x];
        if (!next) break;
        timers.delete(next[0]);
        t = next[1].at;
        next[1].fn();
      }
      t = end;
    },
  };
}

// A fake SpeechRecognition: records instances; tests fire events on them.
function fakeRecognition() {
  const made = [];
  class Fake {
    constructor() { this.started = false; this.aborted = false; made.push(this); }
    start() { this.started = true; }
    abort() { this.aborted = true; }
    fireStart() { if (this.onstart) this.onstart(); }
    say(text, { final = true, confidence = 0.9 } = {}) {
      if (this.onresult) {
        this.onresult({ resultIndex: 0, results: [Object.assign([{ transcript: text, confidence }], { isFinal: final })] });
      }
    }
    error(code) { if (this.onerror) this.onerror({ error: code }); }
    end() { if (this.onend) this.onend(); }
  }
  return { Fake, made, live: () => made.filter((r) => r.started && !r.aborted && r.onend) };
}

function setup() {
  const c = clock();
  const rec = fakeRecognition();
  const lines = [];
  const states = [];
  const L = LC.createListener({
    Recognition: rec.Fake, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout, now: c.now,
    onLine: (t) => lines.push(t), onState: (s, t) => states.push([s, t]),
    cfg: { gapMs: 1200, maxLineMs: 15000, echoMs: 4000 },
  });
  return { c, rec, L, lines, states, current: () => rec.made[rec.made.length - 1] };
}

// --- pure helpers ---------------------------------------------------------------------------

test("isEcho: the reply heard back is an echo, the partner's own short answer is not", () => {
  const reply = "Yes, let's go outside.";
  assert.equal(LC.isEcho("yes let's go outside", reply, 500, 4000), true);
  assert.equal(LC.isEcho("let's go", reply, 500, 4000), true); // two words of it
  assert.equal(LC.isEcho("yes", reply, 500, 4000), false); // the old filter dropped this
  assert.equal(LC.isEcho("no I don't think so", "No.", 500, 4000), false);
  assert.equal(LC.isEcho("yes let's go outside", reply, 5000, 4000), false); // after the window
  assert.equal(LC.isEcho("let's go outside today then", reply, 500, 4000), false);
  assert.equal(LC.isEcho("anything", "", 0, 4000), false);
});

test("joinFinals adds a full stop between sentences without one", () => {
  assert.equal(LC.joinFinals(["it might not work right now", "how was your weekend"]),
               "it might not work right now. how was your weekend");
  assert.equal(LC.joinFinals(["Hello there.", "are you hungry"]), "Hello there. are you hungry");
  assert.equal(LC.joinFinals(["  ", "tea"]), "tea");
});

test("meaningful drops filler and single letters", () => {
  assert.equal(LC.meaningful("um uh"), false);
  assert.equal(LC.meaningful("a"), false);
  assert.equal(LC.meaningful("tea"), true);
  assert.equal(LC.meaningful("um are you okay"), true);
});

// --- the loop ---------------------------------------------------------------------------------

test("want(true) listens until stopped and sends each line after a pause, still listening", () => {
  const { c, L, lines, current } = setup();
  L.want(true, 0);
  c.advance(1);
  const r = current();
  r.fireStart();
  r.say("are you hungry");
  c.advance(1200);
  assert.deepEqual(lines, ["are you hungry"]);
  assert.equal(L.wanted, true);
  assert.equal(r.aborted, false); // the same session keeps listening
  r.say("I brought tea");
  c.advance(1200);
  assert.deepEqual(lines, ["are you hungry", "I brought tea"]);
});

test("calling want(true) again while on does nothing", () => {
  const { c, L, rec } = setup();
  L.want(true, 0);
  c.advance(1);
  L.want(true, 0);
  c.advance(1);
  assert.equal(rec.made.length, 1);
});

test("Chrome ending a session restarts it: 250 ms after a healthy one", () => {
  const { c, L, rec, current } = setup();
  L.want(true, 0);
  c.advance(1);
  current().fireStart();
  c.advance(8000); // silence: Chrome ends the session
  current().error("no-speech");
  current().end();
  c.advance(249);
  assert.equal(rec.made.length, 1);
  c.advance(1);
  assert.equal(rec.made.length, 2);
});

test("sessions that die at once back off to 4 s, never give up, one recognizer at a time", () => {
  const { c, L, rec } = setup();
  L.want(true, 0);
  c.advance(1);
  let lastGap = 0;
  for (let i = 0; i < 50; i++) {
    const r = rec.made[rec.made.length - 1];
    const before = c.now();
    r.end();
    const count = rec.made.length;
    let waited = 0;
    while (rec.made.length === count && waited < 10000) { c.advance(50); waited += 50; }
    lastGap = c.now() - before;
    assert.ok(rec.live().length <= 1);
  }
  assert.ok(lastGap <= 4050 && lastGap >= 3950, `capped backoff, got ${lastGap}`);
  assert.equal(L.wanted, true);
});

test("a start() that throws is retried", () => {
  const c = clock();
  let throws = 2;
  const made = [];
  class Flaky {
    constructor() { made.push(this); }
    start() { if (throws > 0) { throws -= 1; throw new Error("InvalidStateError"); } this.started = true; }
    abort() {}
  }
  const L = LC.createListener({ Recognition: Flaky, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout,
                                now: c.now, cfg: {} });
  L.want(true, 0);
  c.advance(5000);
  assert.ok(made.some((r) => r.started));
});

test("the watchdog restarts a session whose end event never came", () => {
  const { c, L, rec, current } = setup();
  L.want(true, 0);
  c.advance(1);
  const r = current();
  r.fireStart();
  r.onend = null; // the end event is lost
  c.advance(63000); // no event for over 60 s: stalled
  assert.ok(rec.made.length >= 2 && r.aborted);
});

test("a blocked microphone stops the loop and says so; nothing restarts", () => {
  const { c, L, rec, states, current } = setup();
  L.want(true, 0);
  c.advance(1);
  current().error("not-allowed");
  current().end();
  c.advance(20000);
  assert.equal(rec.made.length, 1);
  assert.equal(L.wanted, false);
  assert.equal(states[states.length - 1][0], "denied");
});

test("network errors retry forever with 1, 2, 4, 8, 8 s", () => {
  const { c, L, rec, current } = setup();
  L.want(true, 0);
  c.advance(1);
  const gaps = [];
  for (let i = 0; i < 5; i++) {
    const before = c.now();
    current().error("network");
    current().end();
    const count = rec.made.length;
    while (rec.made.length === count) c.advance(100);
    gaps.push(Math.round((c.now() - before) / 1000));
  }
  assert.deepEqual(gaps, [1, 2, 4, 8, 8]);
});

test("words still being recognised when Chrome ends a session are sent", () => {
  const { c, L, lines, current } = setup();
  L.want(true, 0);
  c.advance(1);
  current().say("do you want", { final: false });
  current().end();
  assert.deepEqual(lines, ["do you want"]);
});

test("a monologue is sent after maxLineMs even without a pause", () => {
  const { c, L, lines, current } = setup();
  L.want(true, 0);
  c.advance(1);
  const r = current();
  for (let i = 0; i < 16; i++) { r.say(`sentence ${i}`); c.advance(1000); }
  assert.ok(lines.length >= 1 && lines[0].startsWith("sentence 0."));
});

test("the reply heard right after reopening is dropped; the partner's 'yes' is kept", () => {
  const { c, L, lines, current } = setup();
  L.heardSpoken("Yes, let's go outside.");
  L.want(true, 0.5);
  c.advance(600);
  current().say("yes let's go outside");
  c.advance(1300);
  current().say("yes");
  c.advance(1300);
  assert.deepEqual(lines, ["yes"]);
});

test("stop() aborts the session, sends what was said, and stays stopped", () => {
  const { c, L, rec, lines, current } = setup();
  L.want(true, 0);
  c.advance(1);
  const r = current();
  r.say("hold on");
  L.stop("speaking");
  assert.equal(r.aborted, true);
  assert.deepEqual(lines, ["hold on"]); // said before the reply started
  c.advance(20000);
  assert.equal(rec.made.length, 1);
});

test("mute drops what was being said", () => {
  const { c, L, lines, current } = setup();
  L.want(true, 0);
  c.advance(1);
  current().say("private words");
  L.stop("muted");
  assert.deepEqual(lines, []);
});

test("a window (turn mode) ends after one line or when its time is up", () => {
  const { c, L, lines, current } = setup();
  L.windowFor(10, 0);
  c.advance(1);
  current().say("are you cold");
  c.advance(1200);
  assert.deepEqual(lines, ["are you cold"]);
  assert.equal(L.wanted, false);
  L.windowFor(10, 0);
  c.advance(11000);
  assert.equal(L.wanted, false);
});

test("kick() restarts at once and resets the backoff", () => {
  const { c, L, rec } = setup();
  L.want(true, 0);
  c.advance(1);
  for (let i = 0; i < 5; i++) { rec.made[rec.made.length - 1].end(); c.advance(5000); }
  const count = rec.made.length;
  L.kick();
  assert.equal(rec.made.length, count + 1);
});

test("no onstart within 3 s reports needs_tap", () => {
  const { c, L, states } = setup();
  L.want(true, 0);
  c.advance(3100);
  assert.ok(states.some(([s]) => s === "needs_tap"));
});

test("without speech recognition it says unsupported", () => {
  const c = clock();
  const states = [];
  const L = LC.createListener({ Recognition: undefined, setTimeout: c.setTimeout, clearTimeout: c.clearTimeout,
                                now: c.now, onState: (s) => states.push(s), cfg: {} });
  L.want(true, 0);
  c.advance(10);
  assert.equal(states[states.length - 1], "unsupported");
});
