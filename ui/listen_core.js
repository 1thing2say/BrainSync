// Partner listening core (PLAN.md V4): pure helpers and the speech-recognition loop. The browser
// pieces (the recognizer class, timers and clock) are passed in, so node can drive the loop with
// fakes (tests/js/listen_core.test.js). Loaded as a plain script before speech.js.
"use strict";

const ListenCore = (() => {
  const FILLER = new Set(["um", "uh", "hmm", "mm", "ah", "oh", "er", "erm"]);
  // How the loop behaves (ms). Pinned by the node tests; not venue tuning, so not in config.
  const T = {
    healthyRestart: 100, // after a normal end (Chrome ends sessions on silence): restart at once,
    //                      or the start of the partner's next sentence is lost
    quickFirst: 500, // a session that died at once (under healthyAfter): back off, doubling...
    quickMax: 4000, // ...up to this
    networkFirst: 1000, // no network: 1, 2, 4, 8 s, then every 8 s, never giving up
    networkMax: 8000,
    audioCapture: 3000, // no microphone: try again every 3 s (a lapel receiver plugged back in)
    healthyAfter: 1000, // a session that ran this long ended normally, not "died at once"
    stallAfter: 60000, // no event at all for this long: abort it and start a new one
    watchdog: 2000, // checks that a session is running or about to
    needsTapAfter: 3000, // no onstart by then: Chrome may want a click first
    graceMs: 3000, // a window ending mid-sentence waits this long for the sentence to finish
  };
  const MIN_CONFIDENCE = 0.3; // Chrome reports 0 when it does not know: those are kept
  const MIN_CARRY_WORDS = 3; // unconfirmed words this long are kept when a session ends
  const FATAL = { "not-allowed": "denied", "service-not-allowed": "denied",
                  "language-not-supported": "unsupported" };

  const norm = (t) => String(t).toLowerCase().replace(/[^a-z0-9' ]/g, " ").replace(/\s+/g, " ").trim();
  const words = (t) => norm(t).split(" ").filter(Boolean);

  // More than filler ("um", "uh") and more than one letter.
  function meaningful(text) {
    const w = words(text).filter((x) => !FILLER.has(x));
    return w.length > 1 || (w.length === 1 && w[0].length > 1);
  }

  // Joins final segments into one line. A segment without closing punctuation gets a "." so the
  // server can tell the sentences apart ("it might not work right now. how was your weekend").
  function joinFinals(finals) {
    const parts = finals.map((f) => String(f).trim()).filter(Boolean);
    return parts.map((f, i) => (i < parts.length - 1 && !/[.!?;,]$/.test(f) ? `${f}.` : f)).join(" ");
  }

  // The laptop's own reply heard back. Only checked shortly after the mic reopened, and only on
  // two or more words in a row, so the partner's own "yes" after "Yes, let's go outside" is kept.
  function isEcho(text, lastSpoken, msSinceReopen, echoMs) {
    if (!lastSpoken || msSinceReopen > echoMs) return false;
    const a = words(text);
    const b = words(lastSpoken);
    const heard = ` ${a.join(" ")} `;
    const reply = ` ${b.join(" ")} `;
    return (a.length >= 2 && reply.includes(heard)) || (b.length >= 2 && heard.includes(reply));
  }

  function createListener(env) {
    const cfg = { gapMs: 1200, maxLineMs: 15000, echoMs: 4000, minConfidence: MIN_CONFIDENCE,
                  lang: "en-US", ...(env.cfg || {}) };
    const later = env.setTimeout;
    const cancel = env.clearTimeout;
    const now = env.now;
    let onLine = env.onLine || (() => {});
    let onState = env.onState || (() => {});

    let wanted = false; // the server allows the mic, or a window is open
    let windowEnd = null; // a window (turn mode, the Listen button): stop then; null = until stopped
    let oneLine = false; // a window closes after one line (the partner's turn is done)
    let rec = null; // the one live recognizer
    let sessionStart = 0;
    let lastEvent = 0;
    let heard = false; // this session heard speech
    let lastError = null;
    let fatal = null;
    let quick = 0;
    let netFails = 0;
    let restarts = 0;
    let finals = [];
    let interim = "";
    let lineOpen = false;
    let lastSpoken = "";
    let reopenedAt = -Infinity;
    let state = "stopped";
    const timers = {};

    function setTimer(name, ms, fn) {
      clear(name);
      timers[name] = later(() => { delete timers[name]; fn(); }, Math.max(0, ms));
    }
    function clear(name) {
      if (timers[name] !== undefined) { cancel(timers[name]); delete timers[name]; }
    }
    function clearAll() { Object.keys(timers).forEach(clear); }

    function report(s, text = "") {
      state = s;
      onState(s, text);
    }

    function detach(r) { r.onstart = r.onresult = r.onerror = r.onend = null; }

    function drop(r) {
      detach(r);
      try { r.abort(); } catch (e) { /* already stopped */ }
    }

    // Sends what was heard as one line. `withInterim`: include words still being recognised
    // (only when they will never become final: the session ended or a window closed).
    function flush(withInterim) {
      clear("gap");
      clear("maxLine");
      const parts = withInterim && interim.trim() ? [...finals, interim] : finals;
      const text = joinFinals(parts);
      finals = [];
      if (withInterim) interim = "";
      lineOpen = Boolean(interim.trim());
      if (!text || !meaningful(text)) return false;
      if (isEcho(text, lastSpoken, now() - reopenedAt, cfg.echoMs)) return false;
      onLine(text);
      report("sent", text);
      if (oneLine) stop("done", true);
      return true;
    }

    function startSession() {
      clear("restart");
      if (!wanted || rec) return;
      if (!env.Recognition) { stop("unsupported"); return; }
      const r = new env.Recognition();
      r.lang = cfg.lang;
      r.continuous = true;
      r.interimResults = true;
      let started = false;
      r.onstart = () => {
        started = true;
        lastEvent = now();
        if (state !== "listening") report("listening");
      };
      r.onresult = (ev) => {
        lastEvent = now();
        heard = true;
        quick = 0;
        netFails = 0;
        interim = "";
        for (let i = ev.resultIndex; i < ev.results.length; i++) {
          const res = ev.results[i];
          const best = res[0];
          if (!res.isFinal) interim += best.transcript;
          else if (best.confidence === 0 || best.confidence >= cfg.minConfidence) finals.push(best.transcript.trim());
        }
        if (!lineOpen && (finals.length || interim.trim())) {
          lineOpen = true;
          setTimer("maxLine", cfg.maxLineMs, () => flush(false)); // a monologue goes out anyway
        }
        clear("gap");
        if (finals.length && !interim.trim()) setTimer("gap", cfg.gapMs, () => flush(false));
        report("listening", joinFinals(interim.trim() ? [...finals, interim] : finals));
      };
      r.onerror = (ev) => {
        lastEvent = now();
        lastError = ev.error;
        if (FATAL[ev.error]) fatal = FATAL[ev.error];
        else if (ev.error === "network") report("offline");
        else if (ev.error === "audio-capture") report("nomic");
        // "no-speech" and "aborted": onend restarts
      };
      r.onend = () => {
        if (rec !== r) return;
        detach(r);
        rec = null;
        clear("tap");
        // A session end is not the end of the partner's line: confirmed words carry over to the
        // next session and go out after the usual pause. Unconfirmed words are less accurate and
        // often cut off, so only a longer run of them is kept.
        if (words(interim).length >= MIN_CARRY_WORDS) finals.push(interim.trim());
        interim = "";
        if (finals.length) setTimer("gap", cfg.gapMs, () => flush(false));
        scheduleRestart();
      };
      rec = r;
      sessionStart = now();
      lastEvent = now();
      heard = false;
      lastError = null;
      try {
        r.start();
      } catch (e) {
        detach(r);
        rec = null;
        lastError = "start-failed";
        scheduleRestart();
        return;
      }
      restarts += 1;
      setTimer("tap", T.needsTapAfter, () => { if (rec === r && !started) report("needs_tap"); });
    }

    function scheduleRestart() {
      if (!wanted) return;
      if (fatal) { stop(fatal); return; }
      if (windowEnd !== null && now() >= windowEnd) { closeWindow(); return; }
      const ran = now() - sessionStart;
      let delay;
      if (lastError === "network") {
        delay = Math.min(T.networkFirst * 2 ** netFails, T.networkMax);
        netFails += 1;
      } else if (lastError === "audio-capture") {
        delay = T.audioCapture;
      } else if (heard || ran >= T.healthyAfter) {
        delay = T.healthyRestart;
        quick = 0;
      } else {
        delay = Math.min(T.quickFirst * 2 ** quick, T.quickMax);
        quick += 1;
      }
      setTimer("restart", delay, startSession);
    }

    function watchdog() {
      if (!wanted) return;
      if (!rec && timers.restart === undefined && timers.delay === undefined) {
        startSession();
      } else if (rec && now() - lastEvent > T.stallAfter) {
        drop(rec);
        rec = null;
        lastError = "stalled";
        scheduleRestart();
      }
      if (wanted) setTimer("watchdog", T.watchdog, watchdog);
    }

    function closeWindow() {
      if (interim.trim()) { // let the sentence finish, then send whatever there is
        setTimer("window", T.graceMs, () => { flush(true); stop("stopped"); });
        return;
      }
      flush(false);
      if (wanted) stop("stopped");
    }

    function open(delayS, seconds) {
      wanted = true;
      fatal = null;
      quick = 0;
      netFails = 0;
      windowEnd = seconds ? now() + (delayS + seconds) * 1000 : null;
      oneLine = Boolean(seconds);
      setTimer("delay", delayS * 1000, () => { reopenedAt = now(); startSession(); });
      setTimer("watchdog", delayS * 1000 + T.watchdog, watchdog);
      if (seconds) setTimer("window", (delayS + seconds) * 1000, closeWindow);
    }

    // Always mode: listen from delayS on until told to stop. Calling it again while on does nothing.
    function want(on, delayS = 0) {
      if (!on) { stop("stopped"); return; }
      if (wanted && windowEnd === null) return;
      stop("restart", true);
      open(delayS, 0);
    }

    // Turn mode and the Listen button: listen for `seconds`, then stop; one line ends the turn.
    function windowFor(seconds, delayS = 0) {
      stop("restart", true);
      open(delayS, seconds);
    }

    function stop(why = "stopped", quiet = false) {
      if (why !== "muted" && why !== "restart" && finals.length) {
        const line = joinFinals(finals); // what the partner said just before the mic closed
        finals = [];
        if (meaningful(line) && !isEcho(line, lastSpoken, now() - reopenedAt, cfg.echoMs)) onLine(line);
      }
      wanted = false;
      windowEnd = null;
      oneLine = false;
      clearAll();
      if (rec) { drop(rec); rec = null; }
      finals = [];
      interim = "";
      lineOpen = false;
      if (!quiet) report(why);
    }

    // Start a session now (the Listen button, a click after needs_tap, a mic plugged in).
    function kick() {
      if (!wanted) return false;
      fatal = null;
      quick = 0;
      netFails = 0;
      if (rec) { drop(rec); rec = null; }
      clear("restart");
      clear("delay");
      startSession();
      return true;
    }

    return {
      want,
      windowFor,
      stop,
      kick,
      flush: () => flush(true),
      heardSpoken(text) { lastSpoken = String(text || ""); },
      configure(c) { Object.assign(cfg, c); },
      onLine(fn) { onLine = fn; },
      onState(fn) { onState = fn; },
      get wanted() { return wanted; },
      get untilStopped() { return wanted && windowEnd === null; },
      get restarts() { return restarts; },
      get state() { return state; },
      get live() { return rec !== null; },
    };
  }

  return { T, MIN_CONFIDENCE, norm, meaningful, joinFinals, isEcho, createListener };
})();

if (typeof module !== "undefined") module.exports = ListenCore;
