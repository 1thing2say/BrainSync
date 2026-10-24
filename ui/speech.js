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

// Speech input: the partner's speech, through ui/listen_core.js (the loop, the echo check and
// the line joining live there, tested with node). The server says when to listen.
const Listener = ListenCore.createListener({
  Recognition: window.SpeechRecognition || window.webkitSpeechRecognition,
  setTimeout: (fn, ms) => window.setTimeout(fn, ms),
  clearTimeout: (id) => window.clearTimeout(id),
  now: () => Date.now(),
});
