// Speech output: the server's pre-generated Cartesia clip when there is one, otherwise the
// browser's speechSynthesis with a local voice (works offline). Speech input: Listener, below.
"use strict";

const Speech = (() => {
  let rate = 1.0;
  let voice = null;
  let audio = null;  // the clip playing now, if any

  // Prefer local voices: Chrome's "Google" voices are network voices and fail offline.
  function pickVoice() {
    const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
    const local = voices.filter((v) => v.localService);
    voice = local.find((v) => v.lang === "en-US")
      || local.find((v) => v.lang && v.lang.startsWith("en"))
      || voices.find((v) => v.lang && v.lang.startsWith("en"))
      || null;
  }

  if (window.speechSynthesis) {
    pickVoice();
    speechSynthesis.onvoiceschanged = pickVoice;
  }

  function synthesize(text, finish) {
    if (!window.speechSynthesis) { finish(); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = rate;
    if (voice) u.voice = voice;
    u.onend = finish;
    u.onerror = finish;
    speechSynthesis.speak(u);
  }

  // Speak text and call done() once, when speech ends, fails, or is unavailable. With audioUrl,
  // play that clip (the same sentence, from Cartesia); if it cannot play, fall back to synthesis.
  function speak(text, done, audioUrl) {
    let finished = false;
    const finish = () => { if (!finished) { finished = true; done(); } };
    stopAudio();
    if (!audioUrl) { synthesize(text, finish); return; }
    const clip = new Audio(audioUrl);
    audio = clip;
    let fellBack = false;
    const fallback = () => {
      if (fellBack || finished || audio !== clip) return;
      fellBack = true;
      audio = null;
      synthesize(text, finish);
    };
    clip.onended = () => { if (audio === clip) audio = null; finish(); };
    clip.onerror = fallback;
    clip.play().catch(fallback);
  }

  function stopAudio() {
    if (audio) {
      audio.onended = audio.onerror = null;
      audio.pause();
      audio = null;
    }
  }

  return {
    speak,
    setRate(r) { rate = r; },
    cancel() {
      stopAudio();
      if (window.speechSynthesis) speechSynthesis.cancel();
    },
  };
})();

// Speech input: Chrome's speech recognition for the conversation partner, hands-free. The server
// says when it is the partner's turn (a "listen" message: the session starts, a reply was just
// spoken) and when to stop (a reply is playing), so nobody presses anything and the app never
// hears itself. Sentences said close together become one line; filler and echoes are dropped.
const Listener = (() => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const GAP_MS = 1200;  // this much silence after speech ends the partner's line
  const GRACE_MS = 3000;  // when the window ends mid-sentence, let the sentence finish
  const MIN_CONFIDENCE = 0.3;  // Chrome reports 0 when it does not know: those are kept
  const FILLER = new Set(["um", "uh", "hmm", "mm", "ah", "oh", "er", "erm"]);
  let rec = null;
  let active = false;
  let timers = {};
  let finals = [];
  let interim = "";
  let lastSpoken = "";
  let onLine = () => {};
  let onUpdate = () => {};

  const norm = (t) => t.toLowerCase().replace(/[^a-z0-9' ]/g, " ").replace(/\s+/g, " ").trim();

  // The reply the laptop just said, heard back through the mic.
  function isEcho(text) {
    const a = norm(text);
    const b = norm(lastSpoken);
    return Boolean(a && b) && (a === b || b.includes(a) || a.includes(b));
  }

  function meaningful(text) {
    const words = norm(text).split(" ").filter((w) => w && !FILLER.has(w));
    return words.length > 1 || (words.length === 1 && words[0].length > 1);
  }

  function clear(...names) {
    for (const n of names) { clearTimeout(timers[n]); delete timers[n]; }
  }

  function report(state) {
    onUpdate(state, [...finals, interim].join(" ").trim());
  }

  function finishLine() {
    const text = finals.join(" ").trim();
    stop("done");
    if (text && meaningful(text) && !isEcho(text)) {
      onLine(text);
      onUpdate("sent", text);
    }
  }

  function open(seconds) {
    finals = [];
    interim = "";
    active = true;
    rec = new Recognition();
    rec.lang = "en-US";
    rec.continuous = true;
    rec.interimResults = true;
    rec.onresult = (ev) => {
      interim = "";
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const best = ev.results[i][0];
        if (!ev.results[i].isFinal) interim += best.transcript;
        else if (best.confidence === 0 || best.confidence >= MIN_CONFIDENCE) finals.push(best.transcript.trim());
      }
      report("listening");
      clear("gap");
      if (finals.length && !interim.trim()) timers.gap = setTimeout(finishLine, GAP_MS);
    };
    rec.onerror = (ev) => {
      if (ev.error === "not-allowed" || ev.error === "service-not-allowed") stop("denied");
      else if (ev.error === "network") stop("offline");
      else if (ev.error === "audio-capture") stop("nomic");
      // "no-speech" and "aborted": onend restarts while the window is open
    };
    rec.onend = () => {
      if (!active) return;
      try { rec.start(); } catch (e) { stop("error"); }  // Chrome closes the mic after silence
    };
    try {
      rec.start();
    } catch (e) {
      stop("error");
      return;
    }
    timers.window = setTimeout(() => {
      if (interim.trim()) timers.window = setTimeout(finishLine, GRACE_MS);
      else if (finals.length) finishLine();
      else stop("timeout");
    }, seconds * 1000);
    report("listening");
  }

  // Listen for up to `seconds`, starting after `delayS` (the tail of a reply that just ended).
  function start(seconds, delayS = 0) {
    if (!Recognition) { onUpdate("unsupported", ""); return; }
    stop("restart");
    timers.delay = setTimeout(() => open(seconds), delayS * 1000);
  }

  function stop(why = "stopped") {
    clear("delay", "window", "gap");
    active = false;
    const r = rec;
    rec = null;
    if (r) {
      r.onresult = r.onerror = r.onend = null;
      try { r.abort(); } catch (e) { /* already stopped */ }
    }
    finals = [];
    interim = "";
    if (why !== "restart" && why !== "done") onUpdate(why, "");
  }

  return {
    supported: Boolean(Recognition),
    start,
    stop,
    heardSpoken(text) { lastSpoken = text; },
    onLine(fn) { onLine = fn; },
    onUpdate(fn) { onUpdate = fn; },
    get listening() { return active; },
  };
})();
