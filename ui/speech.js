// Speech output: the server's pre-generated Cartesia clip when there is one, otherwise the
// browser's speechSynthesis with a local voice (works offline). Speech input: Listener, below.
"use strict";

const Speech = (() => {
  let rate = 1.0;
  let voice = null;
  // One element for every clip: iOS Safari plays audio only from an element a tap has started
  // once (unlock(), on "I understand, start" and Resume); a new Audio() per clip would be blocked.
  const player = new Audio();
  player.preload = "auto";
  let playing = 0;  // the clip playing now (a counter), 0 = none
  let clips = 0;
  let unlocked = false;

  // The best local English voice (ui/voice_core.js): network voices fail offline.
  function pickVoice() {
    voice = VoiceCore.best(window.speechSynthesis ? speechSynthesis.getVoices() : []);
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
    const clip = ++clips;
    playing = clip;
    let fellBack = false;
    const fallback = () => {
      if (fellBack || finished || playing !== clip) return;
      fellBack = true;
      playing = 0;
      synthesize(text, finish);
    };
    player.onended = () => { if (playing === clip) playing = 0; finish(); };
    player.onerror = fallback;
    player.src = audioUrl;
    player.play().catch(fallback);
  }

  function stopAudio() {
    if (playing) {
      player.onended = player.onerror = null;
      player.pause();
      playing = 0;
    }
  }

  // A tenth of a second of silence (8-bit WAV), played from a tap to unlock audio on iOS.
  function silence() {
    const n = 800;
    const b = new Uint8Array(44 + n);
    const v = new DataView(b.buffer);
    const text = (at, s) => [...s].forEach((c, i) => { b[at + i] = c.charCodeAt(0); });
    text(0, "RIFF"); v.setUint32(4, 36 + n, true); text(8, "WAVEfmt ");
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, 8000, true); v.setUint32(28, 8000, true); v.setUint16(32, 1, true);
    v.setUint16(34, 8, true); text(36, "data"); v.setUint32(40, n, true);
    b.fill(128, 44);
    return `data:audio/wav;base64,${btoa(String.fromCharCode(...b))}`;
  }

  // Call from a tap. Afterwards the clip element and speechSynthesis may play without one (iOS).
  function unlock() {
    if (unlocked || playing) return;
    unlocked = true;
    player.src = silence();
    player.play().catch(() => { unlocked = false; });  // try again on the next tap
    if (window.speechSynthesis) {
      const u = new SpeechSynthesisUtterance(" ");
      u.volume = 0;
      speechSynthesis.speak(u);
    }
  }

  return {
    speak,
    unlock,
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
