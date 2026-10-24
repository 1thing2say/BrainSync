// Speech output with the browser's speechSynthesis (works offline). Speech input arrives in V4.
"use strict";

const Speech = (() => {
  let rate = 1.0;
  let voice = null;

  function pickVoice() {
    const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
    voice = voices.find((v) => v.lang === "en-US" && v.localService)
      || voices.find((v) => v.lang && v.lang.startsWith("en"))
      || null;
  }

  if (window.speechSynthesis) {
    pickVoice();
    speechSynthesis.onvoiceschanged = pickVoice;
  }

  // Speak text and call done() once, when speech ends, fails, or is unavailable.
  function speak(text, done) {
    let finished = false;
    const finish = () => { if (!finished) { finished = true; done(); } };
    if (!window.speechSynthesis) { finish(); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = rate;
    if (voice) u.voice = voice;
    u.onend = finish;
    u.onerror = finish;
    speechSynthesis.speak(u);
  }

  return {
    speak,
    setRate(r) { rate = r; },
    cancel() { if (window.speechSynthesis) speechSynthesis.cancel(); },
  };
})();
